"""Conservative, explicit updates of the running Python-package installation.

Receipts are evidence to verify, not authority to replace another installation.
Unknown manager options and unrecorded extras require the owner's manual command.
"""

import contextlib
import fcntl
from importlib import metadata
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

if __package__:
    from . import update_channels as channels
else:
    import update_channels as channels

PackageUpdateError = channels.ChannelError
INDEX = "https://pypi.org/simple"
SIMPLE = INDEX + "/oc-box/"
MAX_FILES = 10000
MAX_CANDIDATES = 12
PIP_FLAGS = [
    "--index-url",
    INDEX,
    "--only-binary=:all:",
    "--no-input",
    "--no-cache-dir",
    "--disable-pip-version-check",
]

# No import of oc_box: the old process must not certify its own replacement.
PROBE = """
import importlib.metadata as m, json, pathlib, sys
seen = {}
for d in m.distributions():
    if d.metadata.get('Name', '').lower().replace('_', '-') != 'oc-box':
        continue
    records = [f for f in d.files or () if len(f.parts) == 2
               and f.parts[0].endswith('.dist-info') and f.name == 'METADATA']
    if len(records) != 1:
        raise SystemExit(2)
    record = pathlib.Path(d.locate_file(records[0])).resolve()
    if not record.is_relative_to(pathlib.Path(sys.prefix).resolve()):
        raise SystemExit(2)
    seen[str(record)] = d
ds = list(seen.values())
if len(ds) != 1:
    raise SystemExit(2)
d = ds[0]
print(json.dumps(dict(prefix=str(pathlib.Path(sys.prefix).resolve()),
    base=str(pathlib.Path(sys.base_prefix).resolve()), version=d.version,
    library=str(pathlib.Path(d.locate_file('')).resolve()),
    package=str(pathlib.Path(d.locate_file('oc_box')).resolve()),
    direct=d.read_text('direct_url.json') is not None,
    requested=d.read_text('REQUESTED') is not None,
    extras=d.metadata.get_all('Provides-Extra', []))))
"""


def _refuse(reason):
    raise PackageUpdateError(
        reason + " No package update was attempted. Use the original pipx/uv tool "
        "command, or your private venv's Python -m pip command, with the intended "
        "extras and an exact version; do not use sudo or system/user-site pip."
    )


def _private(path):
    value = Path(path).stat()
    return value.st_uid == os.geteuid() and not value.st_mode & 0o022


def _read_json(path):
    try:
        mode = path.stat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(mode.st_mode)
            or mode.st_size > 1024 * 1024
        ):
            _refuse("The package-manager receipt is not a bounded regular file.")
        return json.loads(path.read_text())
    except (OSError, ValueError):
        _refuse("The package-manager receipt cannot be validated.")


def _environment(work):
    # Positive allowlist: no provider/publishing credentials, proxy, Python,
    # loader, pip/uv index, keyring, or inherited manager configuration.
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(work / "home"),
        "XDG_CONFIG_HOME": str(work / "config"),
        "XDG_DATA_HOME": str(work / "data"),
        "XDG_CACHE_HOME": str(work / "cache"),
        "LANG": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "PIP_REQUIRE_VIRTUALENV": "1",
        "UV_NO_CONFIG": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_KEYRING_PROVIDER": "disabled",
        "UV_CACHE_DIR": str(work / "uv-cache"),
    }


def _run(argv, work, env, *, timeout=20):
    try:
        result = subprocess.run(
            list(map(str, argv)),
            cwd=work,
            env=env,
            input="",
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PackageUpdateError(
            "The package-manager operation failed or timed out; inspect the "
            "original environment before retrying. Preferences were not changed."
        ) from exc
    if result.returncode or len(result.stdout) > 1024 * 1024:
        # Child errors can include credentials, index URLs or private config.
        raise PackageUpdateError(
            "The package-manager operation failed validation; inspect the "
            "original environment before retrying. Preferences were not changed."
        )
    return result.stdout.strip()


def _probe(python, work, env):
    try:
        value = json.loads(_run([python, "-I", "-c", PROBE], work, env))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, TypeError) as exc:
        raise PackageUpdateError(
            "Fresh Python-package metadata could not be verified; preferences "
            "were not changed."
        ) from exc


def _validate_probe(value, prefix, root, version=None, *, updated=False):
    try:
        valid = (
            Path(value["prefix"]) == prefix
            and value["base"] != str(prefix)
            and Path(value["library"]).is_relative_to(prefix)
            and Path(value["package"]) == root
            and value["direct"] is False
            and (version is None or value["version"] == version)
        )
        channels.parse_version(value["version"])
    except (KeyError, TypeError, ValueError, channels.ChannelError):
        valid = False
    if not valid:
        if updated:
            raise PackageUpdateError(
                "Updated package metadata did not match the intended environment "
                "and exact version; inspect it before retrying. Preferences were not saved."
            )
        _refuse("The installed distribution does not match this private environment.")


def _executable(name, work, env):
    found = shutil.which(name)
    if not found:
        _refuse("The original package manager is not available on PATH.")
    path = Path(found).resolve()
    try:
        mode = path.stat()
        if (
            not stat.S_ISREG(mode.st_mode)
            or mode.st_uid not in (0, os.geteuid())
            or mode.st_mode & 0o022
            or not os.access(path, os.X_OK)
        ):
            _refuse("The package-manager executable is not safely owned.")
    except OSError:
        _refuse("The package-manager executable cannot be verified.")
    version = _run([path, "--version"], work, env)
    pattern = r"uv 0\.\d+\.\d+(?: .*)?" if name == "uv" else r"[12]\.\d+\.\d+(?: .*)?"
    if not re.fullmatch(pattern, version):
        _refuse("This package-manager version is not supported for delegation.")
    return path


def _extras(value):
    if (
        not isinstance(value, list)
        or len(value) > 32
        or any(
            not isinstance(x, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", x)
            for x in value
        )
    ):
        _refuse("The installation's extra requirements are ambiguous.")
    return sorted(set(value))


def _requirement(value):
    if not isinstance(value, str):
        _refuse("The package-manager requirement is invalid.")
    match = re.fullmatch(
        r"oc[-_]box(?:\[([a-z0-9,-]+)\])?"
        r"(?:(?:==|>=|<=|~=|!=|>|<)\d+\.\d+\.\d+(?:\.dev0)?"
        r"(?:,(?:==|>=|<=|~=|!=|>|<)\d+\.\d+\.\d+(?:\.dev0)?)*)?",
        value,
    )
    if not match:
        _refuse(
            "Source, editable or unsupported package requirements are not delegated."
        )
    return _extras(match[1].split(",") if match[1] else [])


def _launcher(path, prefix):
    path = Path(path)
    if (
        path.name != "oc-box"
        or not path.is_symlink()
        or path.resolve() != (prefix / "bin/oc-box").resolve()
        or not _private(path.parent)
    ):
        _refuse("The manager's installed launcher does not select this environment.")
    return path.parent


def _manager(prefix, before, work, env):
    pipx = prefix / "pipx_metadata.json"
    uv = prefix / "uv-receipt.toml"
    if pipx.exists() and uv.exists():
        _refuse("Multiple package managers claim this environment.")
    if pipx.exists():
        receipt = _read_json(pipx)
        try:
            main = receipt["main_package"]
            if (
                receipt["pipx_metadata_version"] not in ("0.4", "0.5")
                or prefix.parent.name != "venvs"
                or prefix.name != "oc-box"
                or main["package"] != "oc-box"
                or main["package_version"] != before["version"]
                or main.get("suffix", "")
                or main.get("pinned", False)
                or receipt.get("venv_args")
                or receipt.get("injected_packages")
                or main.get("pip_args", [])
                not in ([], PIP_FLAGS, ["--force-reinstall", *PIP_FLAGS])
                or main.get("include_dependencies") is not False
                or main.get("include_apps") is not True
            ):
                _refuse(
                    "The pipx receipt has unsupported ownership or manager options."
                )
            extras = _requirement(main["package_or_url"])
            if "oc-box" not in main["apps"]:
                _refuse("The pipx receipt does not own the oc-box application.")
            source = receipt["source_interpreter"]
            if (
                isinstance(source, dict)
                and set(source) == {"__Path__", "__type__"}
                and source["__type__"] == "Path"
            ):
                source = source["__Path__"]
            if (
                not isinstance(source, str)
                or not Path(source).is_file()
                or Path(source).resolve() != (prefix / "bin/python").resolve()
            ):
                _refuse("The original pipx Python interpreter cannot be verified.")
            env["PIPX_DEFAULT_PYTHON"] = source
        except (KeyError, TypeError):
            _refuse("The pipx receipt is incomplete.")
        env["PIPX_HOME"] = str(prefix.parent.parent)
        bin_dir = Path(os.environ.get("PIPX_BIN_DIR", str(Path.home() / ".local/bin")))
        env["PIPX_BIN_DIR"] = str(_launcher(bin_dir / "oc-box", prefix))
        env["PIPX_MAN_DIR"] = os.environ.get(
            "PIPX_MAN_DIR", str(Path.home() / ".local/share/man")
        )
        executable = _executable("pipx", work, env)
        try:
            inventory = json.loads(_run([executable, "list", "--json"], work, env))
            if inventory["venvs"][prefix.name]["metadata"] != receipt:
                _refuse(
                    "The pipx inventory does not match the running package receipt."
                )
            actual = _run(
                [executable, "environment", "--value", "PIPX_LOCAL_VENVS"], work, env
            )
            if Path(actual).resolve() != prefix.parent:
                _refuse("The pipx executable selects a different environment.")
        except (KeyError, ValueError, TypeError):
            _refuse("The pipx environment inventory cannot be verified.")
        return "pipx", executable, extras
    if uv.exists():
        try:
            import tomllib
        except ImportError:
            _refuse("Automatic uv receipt verification needs Python 3.11 or newer.")
        try:
            mode = uv.stat()
            if (
                uv.is_symlink()
                or not stat.S_ISREG(mode.st_mode)
                or mode.st_size > 1024 * 1024
            ):
                _refuse("The uv receipt is not a bounded regular file.")
            receipt = tomllib.loads(uv.read_text())
            tool = receipt["tool"]
            requirements = tool["requirements"]
            if (
                set(receipt) != {"tool"}
                or set(tool) - {"requirements", "entrypoints", "options"}
                or not _uv_options(tool.get("options", {}))
                or len(requirements) != 1
                or set(requirements[0]) - {"name", "extras", "specifier"}
                or requirements[0]["name"] != "oc-box"
                or prefix.name != "oc-box"
            ):
                _refuse("The uv receipt has unsupported ownership or manager options.")
            requirement = requirements[0]
            extras = _extras(requirement.get("extras", []))
            _requirement("oc-box" + requirement.get("specifier", ""))
            points = tool["entrypoints"]
            if (
                len(points) != 1
                or points[0].get("name") != "oc-box"
                or points[0].get("from", "oc-box") != "oc-box"
            ):
                _refuse("The uv receipt has unsupported entry points.")
            env["UV_TOOL_DIR"] = str(prefix.parent)
            env["UV_TOOL_BIN_DIR"] = str(_launcher(points[0]["install-path"], prefix))
        except (OSError, ValueError, KeyError, TypeError):
            _refuse("The uv receipt cannot be validated.")
        executable = _executable("uv", work, env)
        if (
            Path(_run([executable, "tool", "dir", "--no-config"], work, env)).resolve()
            != prefix.parent
        ):
            _refuse("The uv executable selects a different tools directory.")
        inventory = _run(
            [executable, "tool", "list", "--show-paths", "--no-config"], work, env
        )
        expected = f"oc-box v{before['version']} ({prefix})"
        if inventory.splitlines().count(expected) != 1:
            _refuse("The uv inventory does not match the running environment.")
        return "uv", executable, extras
    # pip does not record requested extras. Never infer them merely because an
    # optional dependency happens to be installed in this venv.
    if before["extras"]:
        _refuse(
            "Plain pip did not record the requested extras; automatic preservation is ambiguous."
        )
    if before.get("requested") is not True:
        _refuse("oc-box was not explicitly requested in this private venv.")
    _run([prefix / "bin/python", "-I", "-m", "pip", "--version"], work, env)
    return "pip", prefix / "bin/python", []


def _python_compatible(specifier):
    """Conservative stdlib-only Requires-Python evaluation; unknown means no."""
    if not specifier:
        return True
    if not isinstance(specifier, str) or len(specifier) > 256:
        return False
    current = tuple(sys.version_info[:3])
    for item in specifier.split(","):
        match = re.fullmatch(
            r"\s*(>=|<=|==|!=|~=|>|<)\s*(\d+(?:\.\d+){0,2})(\.\*)?\s*", item
        )
        if not match:
            return False
        op, number, wildcard = match.groups()
        parts = tuple(map(int, number.split(".")))
        other = parts + (0,) * (3 - len(parts))
        if wildcard:
            if op not in ("==", "!="):
                return False
            result = current[: len(parts)] == parts
            result = result if op == "==" else not result
        elif op == "~=":
            if len(parts) < 2:
                return False
            upper = (
                (parts[0] + 1, 0, 0) if len(parts) == 2 else (parts[0], parts[1] + 1, 0)
            )
            result = other <= current < upper
        else:
            result = {
                ">=": current >= other,
                "<=": current <= other,
                ">": current > other,
                "<": current < other,
                "==": current == other,
                "!=": current != other,
            }[op]
        if not result:
            return False
    return True


def _file_version(item):
    if not isinstance(item, dict) or item.get("yanked", False) is not False:
        return None
    if not isinstance(item.get("filename"), str) or not isinstance(
        item.get("url"), str
    ):
        return None
    match = re.fullmatch(
        r"oc_box-(\d+\.\d+\.\d+(?:\.dev0)?)-py3-none-any\.whl",
        item.get("filename", ""),
    )
    if not match or not _python_compatible(
        item.get("requires-python", item.get("requires_python"))
    ):
        return None
    try:
        url = urlsplit(item["url"])
    except ValueError:
        return None
    if (
        url.scheme != "https"
        or url.netloc != "files.pythonhosted.org"
        or url.username
        or url.password
        or url.fragment
        or url.query
    ):
        return None
    return match[1]


def _inventory():
    response = channels.fetch_json(SIMPLE)
    if (
        not isinstance(response, dict)
        or not isinstance(response.get("files"), list)
        or len(response["files"]) > MAX_FILES
    ):
        raise PackageUpdateError(
            "Unexpected or oversized public PyPI package inventory."
        )
    versions = {"stable": set(), "devel": set()}
    for item in response["files"]:
        version = _file_version(item)
        if version:
            try:
                parsed = channels.parse_version(version)
            except channels.ChannelError:
                continue
            versions["devel" if parsed[3] == 0 else "stable"].add(version)
    return versions


def _target(versions, channel, *, required=True):
    for version in sorted(versions[channel], key=channels.parse_version, reverse=True)[
        :MAX_CANDIDATES
    ]:
        record = channels.fetch_json(f"https://pypi.org/pypi/oc-box/{version}/json")
        try:
            info, files = record["info"], record["urls"]
            if (
                info["name"] != "oc-box"
                or info["version"] != version
                or not isinstance(files, list)
                or len(files) > 100
            ):
                raise ValueError()
            if not _python_compatible(info.get("requires_python")):
                continue
            if not any(_file_version(item) == version for item in files):
                continue
            return version, _extras(info.get("provides_extra") or [])
        except (KeyError, TypeError, ValueError) as exc:
            raise PackageUpdateError(
                "Unexpected public PyPI version metadata."
            ) from exc
    if required:
        raise PackageUpdateError(
            "No compatible, unyanked universal wheel was found for this channel; "
            "use the original manager manually if platform-specific installation is needed."
        )
    return None


def _saved_extras(owner):
    settings = channels.load_settings()
    saved = settings.get("package_update_extras", {})
    if not isinstance(saved, dict):
        raise PackageUpdateError("Invalid Python-package extras preferences.")
    return _extras(saved.get(owner, []))


def _uv_options(options):
    """Only defaults and the safe options written by our own delegation."""
    if not isinstance(options, dict) or set(options) - {
        "index",
        "index-url",
        "keyring-provider",
        "no-build",
    }:
        return False
    expected_index = [
        {
            "url": INDEX,
            "explicit": False,
            "default": True,
            "format": "simple",
            "authenticate": "auto",
        }
    ]
    return (
        options.get("index", expected_index) == expected_index
        and options.get("index-url", INDEX) == INDEX
        and options.get("keyring-provider", "disabled") == "disabled"
        and options.get("no-build", True) is True
    )


def _remember(owner, channel, extras):
    def change(settings):
        saved = settings.setdefault("package_update_extras", {})
        packages = settings.setdefault("package_update_channels", {})
        if not isinstance(saved, dict) or not isinstance(packages, dict):
            raise PackageUpdateError("Invalid Python-package update preferences.")
        saved[owner] = extras
        packages[owner] = channel

    channels.mutate_settings(change)


@contextlib.contextmanager
def _operation_lock(prefix, check):
    if check:
        yield
        return
    try:
        fd = os.open(
            prefix.parent
            / (
                ".oc-box-upgrade-"
                + hashlib.sha256(os.fsencode(prefix)).hexdigest()
                + ".lock"
            ),
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        with os.fdopen(fd, "a+") as lock:
            mode = os.fstat(lock.fileno())
            if (
                not stat.S_ISREG(mode.st_mode)
                or mode.st_uid != os.geteuid()
                or mode.st_mode & 0o077
            ):
                raise PackageUpdateError(
                    "The package update lock is not private; no update attempted."
                )
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PackageUpdateError(
                    "Another package update is in progress; retry after it finishes."
                ) from exc
            yield
    except OSError as exc:
        raise PackageUpdateError(
            "Cannot lock this private package environment; no update attempted."
        ) from exc


def upgrade(root, *, check=False, channel=None):
    """Update only a positively verified package owner; never create a launcher."""
    if os.geteuid() == 0:
        raise PackageUpdateError(
            "Run oc-box upgrade as your regular user, without sudo."
        )
    if os.environ.get("OC_BOX_CONTAINER") == "1":
        raise PackageUpdateError(
            "Run oc-box upgrade on the host, outside the workspace container."
        )
    prefix = Path(sys.prefix).resolve()
    root = Path(root).resolve()
    if prefix == Path(sys.base_prefix).resolve():
        _refuse(
            "System and user-site package installations are not updated automatically."
        )
    try:
        configuration = prefix / "pyvenv.cfg"
        cfg = configuration.read_text()
        if (
            not _private(prefix)
            or not _private(prefix / "bin")
            or not _private(root)
            or not _private(root.parent)
            or not _private(configuration)
            or configuration.is_symlink()
            or not re.search(r"(?mi)^include-system-site-packages\s*=\s*false\s*$", cfg)
            or (prefix / "EXTERNALLY-MANAGED").exists()
            or not root.is_relative_to(prefix)
        ):
            _refuse(
                "The environment is shared, externally managed or not privately owned."
            )
        dist = metadata.distribution("oc-box")
        if (
            dist.read_text("direct_url.json") is not None
            or Path(dist.locate_file("oc_box")).resolve() != root
            or not Path(dist.locate_file("")).resolve().is_relative_to(prefix)
        ):
            _refuse(
                "Editable, direct-URL and unrelated source installations need their original owner."
            )
        marker = _read_json(root / "distribution.json")
        if (
            marker.get("installer") != "python-package"
            or marker.get("version") != dist.version
        ):
            _refuse("This root is not the installed oc-box distribution.")
    except (OSError, metadata.PackageNotFoundError, AttributeError):
        _refuse(
            "This root cannot be verified as the running private package installation."
        )
    owner = "package:" + hashlib.sha256(os.fsencode(prefix)).hexdigest()
    with (
        _operation_lock(prefix, check),
        tempfile.TemporaryDirectory(prefix="oc-box-package-update-") as temporary,
    ):
        work = Path(temporary)
        env = _environment(work)
        python = prefix / "bin/python"
        before = _probe(python, work, env)
        _validate_probe(before, prefix, root, dist.version)
        kind, executable, extras = _manager(prefix, before, work, env)
        # The receipt remains authoritative for current extras. Only tui needs a
        # sticky preference because older stable releases cannot record it.
        extras = sorted(set(extras) | (set(_saved_extras(owner)) & {"tui"}))
        previous = channels.selected_channel(owner=owner)
        selected = channels.selected_channel(channel, owner=owner)
        versions = _inventory()
        version, offered_extras = _target(versions, selected)
        current_order = channels.parse_version(before["version"])
        target_order = channels.parse_version(version)
        print("Installed oc-box: " + before["version"], flush=True)
        print(
            f"PyPI {selected}: {version}" + (" (check only)." if check else "."),
            flush=True,
        )
        if selected == "stable":
            try:
                development = _target(versions, "devel", required=False)
                if development and channels.parse_version(development[0]) > max(
                    current_order, target_order
                ):
                    print(
                        f"Newer development version available: {development[0]}; "
                        "use 'oc-box upgrade --devel' to opt in.",
                        flush=True,
                    )
            except channels.ChannelError:
                print(
                    "Development-version warning unavailable; continuing with stable.",
                    file=sys.stderr,
                )
        switched = channel is not None and (channel == "stable" or channel != previous)
        if target_order < current_order and switched:
            print(
                "Warning: explicit channel selection permits a launcher downgrade; "
                "workspace/session state is not rolled back.",
                flush=True,
            )
        if check:
            return None
        if target_order < current_order and not switched:
            print("Installed build is ahead of this channel; leaving it installed.")
            return None
        if target_order == current_order:
            print("oc-box is up to date.")
            if channel is not None:
                _remember(owner, selected, extras)
            return None
        # tui was a development-only extra. Retain the user's preference while
        # omitting it from a stable target that does not advertise it.
        missing = set(extras) - set(offered_extras)
        if missing - {"tui"}:
            _refuse(
                "The target release does not provide all originally requested extras."
            )
        target_extras = sorted(set(extras) & set(offered_extras))
        suffix = "[" + ",".join(target_extras) + "]" if target_extras else ""
        requirement = "oc-box" + suffix + "==" + version
        # A constraint is defense in depth and never becomes a recorded manager
        # option pointing at a deleted temporary file.
        constraint = work / "target.txt"
        constraint.write_text("oc-box==" + version + "\n")
        env["PIP_CONSTRAINT"] = str(constraint)
        if kind == "pipx":
            import shlex

            command = [
                executable,
                "install",
                "--force",
                "--python",
                env["PIPX_DEFAULT_PYTHON"],
                "--pip-args",
                shlex.join(PIP_FLAGS),
                requirement,
            ]
        elif kind == "uv":
            command = [
                executable,
                "tool",
                "install",
                "--force",
                requirement,
                "--no-config",
                "--python",
                python,
                "--default-index",
                INDEX,
                "--keyring-provider",
                "disabled",
                "--no-build",
                "--no-python-downloads",
            ]
        else:
            command = [
                executable,
                "-I",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--upgrade",
                *PIP_FLAGS,
                requirement,
            ]
        _run(command, work, env, timeout=240)
        after = _probe(python, work, env)
        _validate_probe(after, prefix, root, version, updated=True)
        # Recheck the filesystem owner after a manager has replaced metadata.
        if not _private(prefix) or (prefix / "EXTERNALLY-MANAGED").exists():
            raise PackageUpdateError(
                "Updated environment ownership changed; preferences were not saved."
            )
        _remember(owner, selected, extras)
        print("Installed oc-box: " + version, flush=True)
    return None
