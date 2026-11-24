"""Exact named-workspace wire contract shared by the host and isolated workers."""

import json
from pathlib import Path
import re


# Both exact releases were exercised against the same native wire contract.
# No series, normalized alias, next patch, or version learned from a reply.
NAMED_NATIVE_RELEASES = frozenset(("2.3.2", "2.4.1"))


class NamedNativeWireError(ValueError):
    def __init__(self):
        super().__init__("Named native workspace identity could not be verified.")


def named_native_supports(version):
    return type(version) is str and version in NAMED_NATIVE_RELEASES


def named_native_port(endpoint):
    match = (
        re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})/?", endpoint)
        if type(endpoint) is str and len(endpoint) <= 23
        else None
    )
    if match is None or (port := int(match[1])) > 65535:
        raise NamedNativeWireError()
    return port


def named_native_info(info, version, endpoint):
    """Validate the native envelope, projecting only the three public fields."""
    url = f"http://127.0.0.1:{named_native_port(endpoint)}"
    if (
        not named_native_supports(version)
        or type(info) is not dict
        or info.keys()
        != {"version", "pid", "hostname", "urls", "paths", "capabilities"}
        or info["version"] != version
        or type(info["pid"]) is not int
        or not 0 < info["pid"] <= 2**31 - 1
        or info["urls"] != [url]
        or type(hostname := info["hostname"]) is not str
        or not 0 < len(hostname) <= 255
        or not hostname.isprintable()
        or type(paths := info["paths"]) is not dict
        or paths.keys() != {"tmp"}
        or type(tmp := paths["tmp"]) is not str
        or not tmp.startswith("/")
        or len(tmp) > 4096
        or not tmp.isprintable()
        or type(capabilities := info["capabilities"]) is not dict
        or capabilities.keys() != {"persistentPty"}
        or capabilities["persistentPty"] is not True
    ):
        raise NamedNativeWireError()
    return {"version": version, "pid": info["pid"], "urls": [url]}


def named_native_json(body):
    def unique(items):
        value = dict(items)
        if len(items) != len(value):
            raise NamedNativeWireError()
        return value

    def invalid(_):
        raise NamedNativeWireError()

    return json.loads(
        body.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid
    )


def named_native_worker_source(worker):
    """Prefix audited helpers for the exact entrypoints executed using `-c`."""
    return Path(__file__).read_text(encoding="utf-8") + "\n" + worker
