"""Reproduce the audited Podman 6.2.0 local configuration layering in memory."""

import json
import math
import os
from pathlib import Path

if __package__:
    from . import workspace_podman
else:
    import workspace_podman


FILES = ("CONTAINERS_CONF", "CONTAINERS_STORAGE_CONF")
NAMES = ("containers", "storage")
SYSTEM_CONFIG = (
    Path("/usr/share/containers"),
    Path("/etc/containers"),
)
_LIMIT = 32
_BYTES = 256 * 1024

# Repeated structs merge their fields. The audited nested maps contain
# ordinary scalar lists; repeated nested map values are not deep-merged.
_STRUCTS = {
    (),
    ("containers",),
    ("engine",),
    ("network",),
    ("machine",),
    ("secrets",),
    ("storage",),
    ("storage", "options"),
    ("storage", "options", "overlay"),
    ("storage", "options", "vfs"),
    ("storage", "options", "btrfs"),
    ("storage", "options", "zfs"),
}
_MAPS = {
    ("engine", "runtimes"),
    ("engine", "runtimes_flags"),
    ("engine", "volume_plugins"),
    ("storage", "options", "pull_options"),
}


class ConfigError(ValueError):
    """Fixed diagnostic only; configuration values and paths remain private."""


def _require(condition, message):
    if not condition:
        raise ConfigError(message)


def _path(value):
    return (
        isinstance(value, str)
        and value.startswith("/")
        and value != "/"
        and len(value) <= 4096
        and not any(ch in value for ch in "\0\r\n")
        and str(Path(value)) == value
        and ".." not in Path(value).parts
    )


def _dropins(directory, uid):
    try:
        with workspace_podman._path_fd(str(directory), uid, directory=True) as (
            pinned,
            _,
        ):
            fd = os.open(
                ".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=pinned
            )
            try:
                with os.scandir(fd) as entries:
                    found = []
                    for index, entry in enumerate(entries):
                        _require(
                            index < _LIMIT,
                            "The Podman configuration directory exceeds the review limit.",
                        )
                        if entry.name.endswith(".conf") and not entry.is_dir(
                            follow_symlinks=False
                        ):
                            found.append(directory / entry.name)
                    return sorted(found)
            finally:
                os.close(fd)
    except FileNotFoundError:
        return []


def candidates(environment, read_file, *, system=SYSTEM_CONFIG):
    """Snapshot the native first-existing main and basename-selected drop-ins."""
    home = environment.get("HOME", "")
    config = environment.get("XDG_CONFIG_HOME", str(Path(home) / ".config"))
    _require(
        _path(home) and _path(config),
        "The local Podman configuration location is unsupported.",
    )
    uid = os.geteuid()
    user = Path(config) / "containers"
    result = {}

    def add(path, family, *, required=False):
        try:
            data = read_file(path)
        except FileNotFoundError:
            if required:
                raise ConfigError(
                    "An explicit Podman configuration file is missing."
                ) from None
            return False
        _require(
            path not in result and len(result) < _LIMIT,
            "Podman configuration has too many or ambiguous inputs.",
        )
        result[path] = family, data
        return True

    for key, name in zip(FILES, NAMES):
        explicit = environment.get(key)
        if explicit:
            _require(
                _path(explicit), "An explicit Podman configuration path is unsupported."
            )
            add(Path(explicit), name, required=True)
            continue
        priority = (user, *reversed(system))
        for folder in priority:
            if add(folder / (name + ".conf"), name):
                break
        winners = {}
        for folder in priority:
            dropins = [folder / (name + ".conf.d")]
            if folder != user:
                rootless = folder / (name + ".rootless.conf.d")
                dropins[:0] = (rootless / str(uid), rootless)
            for directory in dropins:
                for path in _dropins(directory, uid if folder == user else 0):
                    winners.setdefault(path.name, path)
        # Native gives the higher-priority directory the basename tie, then
        # applies the surviving files in global Go bytewise string order.
        for filename in sorted(winners, key=os.fsencode):
            add(winners[filename], name, required=True)
    _require(
        sum(len(blob) for _, blob in result.values()) <= _BYTES,
        "The Podman configuration snapshot exceeds its size bound.",
    )
    return result


def canonical_fields(fields):
    if type(fields) is list:
        return all(map(canonical_fields, fields))
    if type(fields) is dict:
        return all(
            key.isascii() and key == key.lower() and canonical_fields(value)
            for key, value in fields.items()
        )
    return True


def _scalar(value):
    if type(value) is str:
        return json.dumps(value, ensure_ascii=False).replace("\x7f", r"\u007f")
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int and -(2**63) <= value < 2**63:
        return str(value)
    if type(value) is float and math.isfinite(value):
        return repr(value)
    raise ConfigError("Layered Podman configuration contains an unsupported scalar.")


def _render(value):
    if type(value) is dict:
        return (
            "{ "
            + ", ".join(
                f"{_scalar(key)} = {_render(item)}"
                for key, item in sorted(value.items())
            )
            + " }"
        )
    if type(value) is list:
        # Podman's attributed string lists may carry a sticky append flag.
        # Do not silently flatten those or arbitrary arrays of tables.
        _require(
            all(type(item) not in (dict, list) for item in value),
            "Layered Podman configuration contains unsupported list attributes.",
        )
        return "[" + ", ".join(map(_scalar, value)) + "]"
    return _scalar(value)


def _merge(data, layer, path=()):
    _require(
        path in _STRUCTS or path in _MAPS,
        "Layered Podman configuration repeats an unsupported table.",
    )
    for key, latest in layer.items():
        if key in data:
            previous = data[key]
            _require(
                type(previous) is type(latest),
                "Layered Podman configuration changes a value type.",
            )
            if type(latest) is dict:
                _require(
                    path not in _MAPS,
                    "Layered Podman configuration repeats a nested map value.",
                )
                _merge(previous, latest, path + (key,))
                continue
        data[key] = latest


def flatten(layers, parse):
    if not layers:
        return b""
    if len(layers) == 1:
        return layers[0]
    data = {}
    for blob in layers:
        values = parse(blob.decode("utf-8"))
        # Validate even values later overwritten by another declaration.
        _render(values)
        _merge(data, values)
    combined = "".join(
        f"{_scalar(key)} = {_render(value)}\n" for key, value in sorted(data.items())
    ).encode("utf-8")
    _require(
        len(combined) <= _BYTES and parse(combined.decode("utf-8")) == data,
        "The layered Podman configuration could not be represented safely.",
    )
    return combined
