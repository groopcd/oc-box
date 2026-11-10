"""Public HTTPS/Git updates without forwarding provider/publishing credentials."""

import ast
import fcntl
from importlib import metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlparse

if __package__:
    from . import dependencies
    from .installation import active_release, atomic_file, install_lock, source_file
    from .version_info import get_version_info
else:
    import dependencies
    from installation import active_release, atomic_file, install_lock, source_file
    from version_info import get_version_info

UPSTREAM = "https://github.com/groopcd/oc-box.git"
BRANCH = "master"


class UpdateError(Exception):
    pass


def editable_source_root():
    """PEP 610 editable ownership is distinct from unrelated source checkouts."""
    try:
        value = metadata.distribution("oc-box").read_text("direct_url.json")
        if not value:
            return None
        direct = json.loads(value)
        url = urlparse(direct["url"])
        if (
            direct.get("dir_info", {}).get("editable") is True
            and url.scheme == "file"
            and not url.netloc
        ):
            return Path(unquote(url.path)).resolve()
    except (
        metadata.PackageNotFoundError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        OSError,
    ):
        pass
    return None


def package_managed(root):
    root = Path(root).resolve()
    return (root / "distribution.json").is_file() or editable_source_root() == root


def package_upgrade_message(root=None):
    if root is not None and editable_source_root() == Path(root).resolve():
        return (
            "This is an editable source installation. Update the source checkout and rerun "
            "your development command; the GitHub self-updater is disabled. "
            "Normal packaged installations upgrade through pipx/uv/pip."
        )
    prefix = Path(sys.prefix)
    if (prefix / "pipx_metadata.json").is_file():
        command = "pipx upgrade oc-box"
    elif (prefix / "uv-receipt.toml").is_file():
        command = "uv tool upgrade oc-box"
    else:
        import shlex

        command = shlex.join(
            [sys.executable, "-m", "pip", "install", "--upgrade", "oc-box"]
        )
    return (
        "This oc-box installation is Python-package managed. Update with: "
        + command
        + ". GitHub/curl installations still use 'oc-box upgrade'; choose one launcher on PATH."
    )


def public_git_env(temporary):
    # A positive allowlist prevents inherited credential helpers, token vars,
    # Git overrides, proxies, loader hooks, or project config from influencing
    # this anonymous fetch. The temporary HOME has no auth/config files.
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(temporary),
        "XDG_CONFIG_HOME": str(Path(temporary) / "config"),
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "PYTHONNOUSERSITE": "1",
    }
    return env


def git(arguments, temporary, *, timeout=30):
    command = [
        "git",
        "-c",
        "credential.helper=",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "http.followRedirects=false",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.https.allow=always",
        *arguments,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=temporary,
            env=public_git_env(temporary),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise UpdateError("Updating requires Git installed on the host.") from exc
    except subprocess.TimeoutExpired as exc:
        raise UpdateError(
            f"GitHub update operation exceeded {timeout}s; existing installation retained."
        ) from exc
    if result.returncode:
        raise UpdateError("GitHub public fetch failed: " + result.stderr.strip()[:1500])
    return result.stdout.strip()


def current_install(root):
    target = Path.home() / ".local/share/oc-box"
    try:
        active_release()
    except (OSError, ValueError) as exc:
        raise UpdateError(
            "Installed launcher requires explicit recovery; inspect retained installation evidence before updating."
        ) from exc
    return get_version_info(target if (target / "build-info.json").is_file() else root)


def coordinated_installer(source):
    """Published source must opt into the guard; never infer it from a comment.

    This checks a trusted, pinned download's declared protocol, not arbitrary
    code's semantics. The installer remains responsible for enforcing its guard.
    """
    try:
        path = source_file(source, "installation.py")
        if not path.is_file() or path.is_symlink():
            return False
        tree = ast.parse(path.read_text())
    except (OSError, ValueError, SyntaxError):
        return False
    return any(
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "UPGRADE_PROTOCOL"
        and isinstance(node.value, ast.Constant)
        and type(node.value.value) is int
        and node.value.value == 1
        for node in tree.body
    )


def upgrade(root, *, check=False):
    if package_managed(root):
        raise UpdateError(package_upgrade_message(root))
    if os.geteuid() == 0:
        raise UpdateError(
            "Run oc-box upgrade on the host as your regular user, without sudo."
        )
    target = Path.home() / ".local/share/oc-box"
    target.mkdir(parents=True, exist_ok=True)
    with (target / ".upgrade.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _upgrade(root, check=check)


def _upgrade(root, *, check=False):
    """The caller owns .upgrade.lock; publication itself owns .install.lock."""
    if os.geteuid() == 0:
        raise UpdateError(
            "Run oc-box upgrade on the host as your regular user, without sudo."
        )
    with install_lock():
        current = current_install(root)
    try:
        dependencies.require("github-upgrade")
    except dependencies.DependencyError as exc:
        raise UpdateError(str(exc)) from exc
    print("Installed oc-box: " + current["version"], flush=True)
    with tempfile.TemporaryDirectory(prefix="oc-box-upgrade-") as temporary:
        work = Path(temporary)
        remote = git(
            ["ls-remote", "--exit-code", UPSTREAM, "refs/heads/" + BRANCH],
            work,
            timeout=15,
        )
        match = re.fullmatch(r"([0-9a-f]{40})\s+refs/heads/" + BRANCH, remote)
        if not match:
            raise UpdateError(
                "Unexpected GitHub branch response; existing installation retained."
            )
        commit = match[1]
        # Even a matching remote result must not use the pre-network snapshot:
        # a manual publisher may have changed or interrupted activation meanwhile.
        with install_lock():
            current = current_install(root)
            if current.get("commit") == commit and not current.get("dirty"):
                print("oc-box is up to date.")
                return None
        print(
            "GitHub has a different version at "
            + commit[:12]
            + (" (check only)." if check else "; downloading full source history."),
            flush=True,
        )
        if check:
            return None
        source = work / "source"
        git(
            [
                "clone",
                "--quiet",
                "--no-checkout",
                "--single-branch",
                "--branch",
                BRANCH,
                UPSTREAM,
                str(source),
            ],
            work,
            timeout=180,
        )
        git(
            ["-C", str(source), "checkout", "--quiet", "--detach", commit],
            work,
            timeout=30,
        )
        verified_commit = git(["-C", str(source), "rev-parse", "HEAD"], work)
        try:
            count = int(git(["-C", str(source), "rev-list", "--count", "HEAD"], work))
        except ValueError as exc:
            raise UpdateError(
                "Downloaded history has an invalid commit count; existing installation retained."
            ) from exc
        dirty = git(
            ["-C", str(source), "status", "--porcelain", "--untracked-files=no"], work
        )
        if verified_commit != commit or dirty or count < 1:
            raise UpdateError(
                "Downloaded source identity could not be verified; existing installation retained."
            )
        # A manual installer may have advanced the active release while Git was
        # downloading. Read under its publication lock, not the initial snapshot.
        with install_lock():
            current = current_install(root)
            if (
                current.get("commit_count") is not None
                and current["commit_count"] > count
            ):
                print(
                    "Installed build is ahead of the published branch; leaving it installed."
                )
                return None
            if current.get("commit") == commit and not current.get("dirty"):
                print("oc-box is up to date.")
                return None
        if not coordinated_installer(source):
            raise UpdateError(
                "Downloaded installer does not declare the coordinated update protocol; existing installation retained. Review and install that source manually if intended."
            )
        # Version series/baselines belong to the downloaded release, not to
        # this older updater. Its installer embeds the exact source identity.
        print("Installing verified source " + commit[:12], flush=True)
        # A future version may add installed modules; use that version's staged
        # installer. This trusts the published source, but never project hooks,
        # Git credential/config overrides, or inherited provider credentials.
        environment = public_git_env(work)
        environment["HOME"] = str(Path.home())
        # The downloaded installer repeats the ordering check while holding its
        # own publication lock, closing the check-to-subprocess race. No CLI
        # protocol change: old released updater callers still invoke install.py.
        environment["OC_BOX_UPGRADE_EXPECTED_COMMIT"] = commit
        current_root = (Path.home() / ".local/share/oc-box/current").resolve()
        extra = (
            ["--with-vault"] if (current_root / "vault-support.json").is_file() else []
        )
        try:
            subprocess.run(
                [sys.executable, "-B", str(source / "install.py"), *extra],
                cwd=work,
                env=environment,
                check=True,
                timeout=240,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise UpdateError(
                "Installing the downloaded version failed; check the retained installation before retrying."
            ) from exc
        target = Path.home() / ".local/share/oc-box"
        try:
            with install_lock():
                published_release = active_release()
                actual = get_version_info(target)
        except (OSError, ValueError) as exc:
            raise UpdateError(
                "Installed launcher requires explicit recovery after the update operation."
            ) from exc
        if actual.get("commit_count") is not None and actual["commit_count"] > count:
            print(
                "Installed build advanced while downloading; leaving it installed.",
                flush=True,
            )
            return None
        if actual.get("commit") != commit or actual.get("dirty"):
            raise UpdateError("Installed version does not match the downloaded commit.")
        print("Installed oc-box: " + actual["version"], flush=True)
        return published_release or (target / "current").resolve(strict=True)


def update_settings(enabled):
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    target = base / "oc-box/config.json"
    try:
        settings = json.loads(target.read_text()) if target.exists() else {}
    except (OSError, ValueError) as exc:
        raise UpdateError(
            "Cannot read oc-box config; no auto-update setting was changed."
        ) from exc
    if not isinstance(settings, dict):
        raise UpdateError("oc-box config must be a JSON object.")
    settings["auto_update"] = enabled
    atomic_file(target, (json.dumps(settings, indent=2) + "\n").encode(), 0o600)
    print(
        "Automatic updates "
        + ("enabled for normal launches." if enabled else "disabled.")
    )


def automatic_upgrade(root):
    if (
        package_managed(root)
        or os.environ.get("OC_BOX_UPDATE_CHECKED") == "1"
        or (Path(root) / ".git").exists()
        or (Path(root) / ".git").is_symlink()
    ):
        return None
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    try:
        settings = json.loads((base / "oc-box/config.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(settings, dict) or settings.get("auto_update") is not True:
        return None
    # Serialize checks/installations but never block an ordinary workspace
    # launch behind another process downloading an update.
    target = Path.home() / ".local/share/oc-box"
    target.mkdir(parents=True, exist_ok=True)
    with (target / ".upgrade.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        return _upgrade(root)
