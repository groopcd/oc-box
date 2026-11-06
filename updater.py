"""Public HTTPS/Git updates without forwarding provider/publishing credentials."""

import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

if __package__:
    from .installation import atomic_file
    from .version_info import get_version_info
else:
    from installation import atomic_file
    from version_info import get_version_info

UPSTREAM = "https://github.com/groopcd/oc-box.git"
BRANCH = "master"


class UpdateError(Exception):
    pass


def package_managed(root):
    return (Path(root) / "distribution.json").is_file()


def package_upgrade_message():
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
    return get_version_info(target if (target / "build-info.json").is_file() else root)


def upgrade(root, *, check=False):
    if package_managed(root):
        raise UpdateError(package_upgrade_message())
    if os.geteuid() == 0:
        raise UpdateError(
            "Run oc-box upgrade on the host as your regular user, without sudo."
        )
    current = current_install(root)
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
        verified = {
            "commit": commit,
            "commit_count": count,
            "version": f"0.1.{count}+g{commit[:7]}",
        }
        if (
            current.get("commit_count") is not None
            and current["commit_count"] > verified["commit_count"]
        ):
            print(
                "Installed build is ahead of the published branch; leaving it installed."
            )
            return None
        print("Installing " + verified["version"], flush=True)
        # A future version may add installed modules; use that version's staged
        # installer. This trusts the published source, but never project hooks,
        # Git credential/config overrides, or inherited provider credentials.
        environment = public_git_env(work)
        environment["HOME"] = str(Path.home())
        try:
            subprocess.run(
                [sys.executable, "-B", str(source / "install.py")],
                cwd=work,
                env=environment,
                check=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise UpdateError(
                "Installing the downloaded version failed; check the retained installation before retrying."
            ) from exc
        target = Path.home() / ".local/share/oc-box"
        actual = get_version_info(target)
        if actual.get("commit") != commit or actual.get("dirty"):
            raise UpdateError("Installed version does not match the downloaded commit.")
        return (target / "current").resolve(strict=True)


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
        return upgrade(root)
