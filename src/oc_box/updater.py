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
    from .installation import atomic_file
    from .version_info import get_version_info
    from .update_channels import (
        ChannelError,
        available_releases,
        load_settings,
        mutate_settings,
        parse_version,
        remember_channel,
        selected_channel,
        version_channel,
    )
else:
    import dependencies
    from installation import atomic_file
    from version_info import get_version_info
    from update_channels import (
        ChannelError,
        available_releases,
        load_settings,
        mutate_settings,
        parse_version,
        remember_channel,
        selected_channel,
        version_channel,
    )

UPSTREAM = "https://github.com/groopcd/oc-box.git"
BRANCH = "master"  # Legacy public constant; channel refs are always allowlisted.


UpdateError = ChannelError


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
        raise UpdateError("GitHub public fetch failed; existing installation retained.")
    return result.stdout.strip()


def current_install(root):
    target = Path.home() / ".local/share/oc-box"
    return get_version_info(target if (target / "build-info.json").is_file() else root)


def upgrade(root, *, check=False, channel=None):
    managed = package_managed(root)
    if managed and editable_source_root() == Path(root).resolve():
        raise UpdateError(package_upgrade_message(root))
    if os.geteuid() == 0:
        raise UpdateError(
            "Run oc-box upgrade on the host as your regular user, without sudo."
        )
    if managed:
        if __package__:
            from . import package_updates
        else:
            import package_updates
        return package_updates.upgrade(root, check=check, channel=channel)
    if check:
        return _github_upgrade(root, check=True, channel=channel)
    try:
        dependencies.require("github-upgrade")
    except dependencies.DependencyError as exc:
        raise UpdateError(str(exc)) from exc
    target = Path.home() / ".local/share/oc-box"
    if target.is_symlink():
        raise UpdateError("The oc-box installation root must not be a symlink.")
    target.mkdir(parents=True, exist_ok=True)
    with (target / ".upgrade.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError(
                "Another oc-box upgrade is in progress; retry later."
            ) from exc
        return _github_upgrade(root, channel=channel)


def _github_upgrade(root, *, check=False, channel=None):
    previous_channel = selected_channel()
    chosen = selected_channel(channel)
    # Validate settings before network or installation, including --check.
    requested = channel is not None
    switching = requested and (chosen == "stable" or chosen != previous_channel)
    current = current_install(root)
    try:
        dependencies.require("github-upgrade")
    except dependencies.DependencyError as exc:
        raise UpdateError(str(exc)) from exc
    print("Installed oc-box: " + current["version"], flush=True)
    releases = available_releases()
    if releases.get("devel_check_failed"):
        print("Warning: development availability could not be checked.", flush=True)
    release = releases[chosen]
    if release is None:
        raise UpdateError(
            f"No published {chosen} release is available; installation retained."
        )
    desired = release["version"]
    print(
        f"Selected {chosen} release: {desired}" + (" (check only)." if check else "."),
        flush=True,
    )
    if chosen == "stable" and releases["devel"] is not None:
        newer = releases["devel"]["version"]
        if parse_version(newer) > max(
            parse_version(desired), parse_version(current["version"])
        ):
            print(
                f"Development release {newer} is available. Run 'oc-box upgrade --devel' to switch."
            )
    older = parse_version(desired) < parse_version(current["version"])
    if older and not switching:
        print(
            "Installed build is ahead of the published release; leaving it installed. Use an explicit channel selector to switch."
        )
        return None
    if older:
        print(
            "Channel switch will downgrade the launcher; workspace data is not rolled back.",
            flush=True,
        )
    with tempfile.TemporaryDirectory(prefix="oc-box-upgrade-") as temporary:
        work = Path(temporary)
        tag_ref = "refs/tags/" + release["tag"]
        remote = git(
            ["ls-remote", "--exit-code", UPSTREAM, tag_ref, tag_ref + "^{}"],
            work,
            timeout=15,
        )
        commit = resolve_tag(remote, tag_ref)
        if current.get("commit") == commit and not current.get("dirty"):
            if current["version"].split("+", 1)[0] != desired:
                raise UpdateError("Installed commit has a different release identity.")
            if requested and not check:
                remember_channel(chosen)
            print("oc-box is up to date.")
            return None
        print(
            "GitHub has a different release at "
            + commit[:12]
            + (" (check only)." if check else "; downloading full source history."),
            flush=True,
        )
        if check:
            return None
        branch = {"stable": "master", "devel": "devel"}[chosen]
        source = work / "source"
        git(
            [
                "clone",
                "--quiet",
                "--no-checkout",
                "--single-branch",
                "--branch",
                branch,
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
        if (
            not switching
            and current.get("commit_count") is not None
            and current["commit_count"] > count
        ):
            print(
                "Installed build is ahead of the published release; leaving it installed."
            )
            return None
        # A tag is not proof of channel lineage. Frozen ancestors of the fixed
        # channel branch are accepted, but arbitrary release targets are not.
        git(
            [
                "-C",
                str(source),
                "merge-base",
                "--is-ancestor",
                commit,
                "refs/remotes/origin/" + branch,
            ],
            work,
        )
        verify_release_source(source, count, desired, chosen)
        # Version series/baselines belong to the downloaded release, not to
        # this older updater. Its installer embeds the exact source identity.
        print("Installing verified source " + commit[:12], flush=True)
        # A future version may add installed modules; use that version's staged
        # installer. This trusts the published source, but never project hooks,
        # Git credential/config overrides, or inherited provider credentials.
        environment = public_git_env(work)
        environment["HOME"] = str(Path.home())
        # This is a selector, not host configuration contents or credentials.
        if "XDG_CONFIG_HOME" in os.environ:
            environment["XDG_CONFIG_HOME"] = os.environ["XDG_CONFIG_HOME"]
        else:
            environment.pop("XDG_CONFIG_HOME", None)
        current_root = (Path.home() / ".local/share/oc-box/current").resolve()
        features = optional_features(current_root)
        supported = installer_options(source)
        extra = [
            "--with-" + name
            for name, enabled in features.items()
            if enabled and "--with-" + name in supported
        ]
        try:
            subprocess.run(
                [sys.executable, "-B", str(source / "install.py"), *extra],
                cwd=work,
                env=environment,
                check=True,
                timeout=480,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise UpdateError(
                "Installing the downloaded version failed; check the retained installation before retrying."
            ) from exc
        target = Path.home() / ".local/share/oc-box"
        actual = get_version_info(target)
        if (
            actual.get("commit") != commit
            or actual.get("dirty")
            or actual["version"].split("+", 1)[0] != desired
        ):
            raise UpdateError("Installed version does not match the downloaded commit.")
        try:
            atomic_file(
                target / "update-features.json",
                (json.dumps(features) + "\n").encode(),
                0o600,
            )
            remember_channel(chosen)
        except (OSError, UpdateError) as exc:
            raise UpdateError(
                "Release installed, but the channel preference could not be saved; retry with the explicit channel selector."
            ) from exc
        print("Installed oc-box: " + actual["version"], flush=True)
        return (target / "current").resolve(strict=True)


def resolve_tag(remote, ref):
    refs = {}
    for line in remote.splitlines():
        match = re.fullmatch(
            r"([0-9a-f]{40})\s+(" + re.escape(ref) + r"(?:\^\{\})?)", line
        )
        if not match or match[2] in refs:
            raise UpdateError(
                "Unexpected GitHub tag response; existing installation retained."
            )
        refs[match[2]] = match[1]
    if ref not in refs:
        raise UpdateError(
            "Unexpected GitHub tag response; existing installation retained."
        )
    return refs.get(ref + "^{}", refs[ref])


def installer_options(source):
    installer = source / "src/oc_box/install.py"
    if not installer.is_file():
        installer = source / "install.py"
    try:
        return {
            node.value
            for node in ast.walk(ast.parse(installer.read_text()))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
    except (OSError, SyntaxError) as exc:
        raise UpdateError("Cannot validate downloaded installer capabilities.") from exc


def verify_release_source(source, count, desired, channel):
    """Read literal version policy without executing downloaded Python."""
    path = source / "src/oc_box/version_info.py"
    if not path.is_file():
        path = source / "version_info.py"
    try:
        assignments = {}
        for node in ast.parse(path.read_text()).body:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                name = node.targets[0].id
                if name in (
                    "VERSION_SERIES",
                    "VERSION_BASE_COMMIT_COUNT",
                    "RELEASE_CHANNEL",
                ):
                    assignments[name] = ast.literal_eval(node.value)
        series = assignments["VERSION_SERIES"]
        baseline = assignments["VERSION_BASE_COMMIT_COUNT"]
        # A channel switch must not install a pre-bridge updater which forgets
        # the saved channel and no longer understands --stable/--devel.
        source_channel = assignments["RELEASE_CHANNEL"]
        if (
            not isinstance(series, str)
            or type(baseline) is not int
            or baseline < 0
            or count < baseline
        ):
            raise ValueError()
        expected = f"{series}.{count - baseline}" + (
            ".dev0" if source_channel == "devel" else ""
        )
        if (
            source_channel != channel
            or expected != desired
            or version_channel(desired) != channel
        ):
            raise ValueError()
    except (OSError, ValueError, SyntaxError, KeyError) as exc:
        raise UpdateError(
            "Downloaded version/channel does not match the published release; installation retained."
        ) from exc


def optional_features(current_root):
    target = Path.home() / ".local/share/oc-box/update-features.json"
    try:
        saved = json.loads(target.read_text()) if target.exists() else {}
        if not isinstance(saved, dict) or any(
            type(v) is not bool for v in saved.values()
        ):
            raise ValueError()
    except (OSError, ValueError) as exc:
        raise UpdateError("Cannot read retained optional-feature preferences.") from exc
    return {
        feature: saved.get(feature, False)
        or (current_root / (feature + "-support.json")).is_file()
        for feature in ("vault", "tui")
    }


def update_settings(enabled):
    mutate_settings(lambda settings: settings.update(auto_update=enabled))
    print(
        "Automatic updates "
        + ("enabled for normal launches." if enabled else "disabled.")
    )


def automatic_upgrade(root):
    if (
        os.geteuid() == 0
        or package_managed(root)
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
    if target.is_symlink():
        raise UpdateError("The oc-box installation root must not be a symlink.")
    target.mkdir(parents=True, exist_ok=True)
    with (target / ".upgrade.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        return _github_upgrade(root)
