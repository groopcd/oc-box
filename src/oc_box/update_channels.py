"""Bounded anonymous release discovery and private, installation-scoped choices."""

import fcntl
import json
import os
from pathlib import Path
import re
from urllib import error, request

if __package__:
    from .installation import atomic_file
else:
    from installation import atomic_file

CHANNEL_BRANCHES = {"stable": "master", "devel": "devel"}
GITHUB_RELEASES = "https://api.github.com/repos/groopcd/oc-box/releases"
PUBLIC_VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(\.dev0)?")


class ChannelError(Exception):
    pass


def parse_version(value):
    """Order our deliberately small public-version grammar, without dependencies."""
    if not isinstance(value, str):
        raise ChannelError("Invalid oc-box release version.")
    public, _, local = value.partition("+")
    match = PUBLIC_VERSION.fullmatch(public)
    if not match or (local and not re.fullmatch(r"g[a-zA-Z0-9]+(?:\.dirty)?", local)):
        raise ChannelError("Invalid oc-box release version.")
    return (*map(int, match.group(1, 2, 3)), 0 if match[4] else 1)


def version_channel(version):
    return "devel" if parse_version(version)[3] == 0 else "stable"


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ChannelError("Release discovery refused an unexpected redirect.")


def fetch_json(url):
    # No proxy discovery, netrc, ambient tokens, or arbitrary supplied URLs.
    if not (
        url == "https://pypi.org/simple/oc-box/"
        or re.fullmatch(r"https://pypi\.org/pypi/oc-box/[\d.]+(?:dev0)?/json", url)
        or url == GITHUB_RELEASES
        or re.fullmatch(re.escape(GITHUB_RELEASES) + r"\?per_page=100&page=\d+", url)
    ):
        raise ChannelError("Release discovery refused an unexpected endpoint.")
    headers = {
        "User-Agent": "oc-box-update",
        "Accept": (
            "application/vnd.pypi.simple.v1+json"
            if "/simple/" in url
            else "application/json"
        ),
    }
    try:
        opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
        with opener.open(request.Request(url, headers=headers), timeout=15) as response:
            data = response.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            raise ChannelError("Release discovery exceeded its response limit.")
        return json.loads(data)
    except (OSError, ValueError, error.URLError) as exc:
        raise ChannelError(
            "Public release discovery failed; installation retained."
        ) from exc


def available_releases():
    """Return the highest published release per channel, not API date ordering."""
    selected = {"stable": None, "devel": None}
    for page in range(1, 11):
        records = fetch_json(GITHUB_RELEASES + f"?per_page=100&page={page}")
        if not isinstance(records, list) or len(records) > 100:
            raise ChannelError("Unexpected GitHub release response.")
        for record in records:
            if not isinstance(record, dict):
                raise ChannelError("Unexpected GitHub release response.")
            if record.get("draft") is not False:
                continue
            tag = record.get("tag_name")
            if not isinstance(tag, str) or not tag.startswith("v"):
                continue
            version = tag[1:]
            try:
                ordering = parse_version(version)
            except ChannelError:
                continue  # Historical or unrelated tags are not channel releases.
            if "+" in version:
                continue
            channel = version_channel(version)
            if record.get("prerelease") is not (channel == "devel"):
                continue
            previous = selected[channel]
            if previous is None or ordering > parse_version(previous["version"]):
                selected[channel] = {"version": version, "tag": tag, "channel": channel}
        if len(records) < 100:
            return selected
    raise ChannelError("GitHub release inventory exceeded its pagination limit.")


def config_path():
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "oc-box/config.json"


def load_settings():
    target = config_path()
    try:
        # Refuse nonregular configuration selectors rather than following a link
        # when subsequently committing a preference.
        if target.is_symlink():
            raise ChannelError("The oc-box config must not be a symlink.")
        settings = json.loads(target.read_text()) if target.exists() else {}
    except (OSError, ValueError) as exc:
        raise ChannelError(
            "Cannot read oc-box config; no update setting was changed."
        ) from exc
    if not isinstance(settings, dict):
        raise ChannelError("oc-box config must be a JSON object.")
    return settings


def save_settings(settings):
    if not isinstance(settings, dict):
        raise ChannelError("oc-box config must be a JSON object.")
    try:
        atomic_file(
            config_path(), (json.dumps(settings, indent=2) + "\n").encode(), 0o600
        )
    except OSError as exc:
        raise ChannelError("Cannot save oc-box update preferences.") from exc


def _validate(channel):
    if channel not in CHANNEL_BRANCHES:
        raise ChannelError("The update channel must be 'stable' or 'devel'.")
    return channel


def selected_channel(requested=None, *, owner="github"):
    settings = load_settings()
    packages = settings.get("package_update_channels", {})
    if not isinstance(packages, dict):
        raise ChannelError("Invalid Python-package update preferences.")
    saved = (
        settings.get("update_channel", "stable")
        if owner == "github"
        else packages.get(owner, "stable")
    )
    # Invalid saved settings never become refs or an implicit development opt-in.
    _validate(saved)
    return _validate(requested) if requested is not None else saved


def remember_channel(channel, *, owner="github"):
    _validate(channel)
    target = config_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(
            target.parent / ".update-settings.lock", "a+", opener=_private_open
        ) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            settings = load_settings()
            if owner == "github":
                settings["update_channel"] = channel
            else:
                packages = settings.setdefault("package_update_channels", {})
                if not isinstance(packages, dict):
                    raise ChannelError("Invalid Python-package update preferences.")
                packages[owner] = channel
            save_settings(settings)
    except OSError as exc:
        raise ChannelError("Cannot save oc-box update preferences.") from exc


def _private_open(path, flags):
    return os.open(path, flags | os.O_NOFOLLOW, 0o600)
