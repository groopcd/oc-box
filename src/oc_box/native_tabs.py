"""Read-only native tab membership; never history inventory or last-active state.

Contract checked in native v2.2.6, v2.2.7 and v2.3.0 sources:
packages/tui/src/context/{session-tabs,storage}.tsx and config/index.tsx.
Standard published builds use channel ``latest``. cli.json chooses the scope;
state/opencode/latest/tui/tabs.json stores ordered membership, NOT active focus.
Only the separate private container home is inspected. Native owns every write.
"""

import json
import math
import os
import re
import stat
import subprocess
import sys
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

# Opening a browser is explicit browsing. Using saved membership to choose an
# automatic startup/default session retains its separately audited versions.
AUTO_RESUME_RELEASES = frozenset(("2.2.6", "2.2.7", "2.3.0"))
MAX_PREFERENCES = 256 * 1024
MAX_STATE = 1024 * 1024
MAX_TABS = 4096
MAX_ID = 128
MAX_REPLY = MAX_STATE
STATUSES = frozenset(("missing", "valid", "invalid"))
REASONS = frozenset(
    (
        "absent",
        "scope-absent",
        "disabled",
        "legacy",
        "unsupported-version",
        "unsafe-path",
        "invalid-preferences",
        "invalid-state",
        "transport",
    )
)
MISSING_REASONS = frozenset(("absent", "scope-absent", "disabled", "legacy"))


@dataclass(frozen=True)
class Snapshot:
    status: str
    tabs: tuple = ()
    scope: str = "cwd"
    reason: str = None


class TabError(ValueError):
    """Fixed refusal, without private paths, titles, data or native errors."""


def supports_web_tabs(release):
    """Eligibility only: saved state and native replies must still validate."""
    return (
        isinstance(release, str)
        and re.fullmatch(r"2\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", release)
        is not None
    )


def _id(value):
    return (
        isinstance(value, str)
        and len(value) <= MAX_ID
        and re.fullmatch(r"ses[A-Za-z0-9_-]+", value) is not None
    )


def _bad(reason):
    return Snapshot("invalid", reason=reason)


def _json(text, *, comments=False):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    def constant(_value):
        raise ValueError("constant")

    if comments:
        # Match quoted strings first so comment markers inside strings survive.
        tokens = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/')
        text = tokens.sub(lambda m: m[0] if m[0].startswith('"') else " ", text)
        text = re.sub(
            r'"(?:\\.|[^"\\])*"|,(\s*[}\]])',
            lambda m: m[0] if m[0].startswith('"') else m[1],
            text,
        )
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def _read(home, relative, limit):
    """Pin each parent with openat; never follow links, open devices or block."""
    uid = os.geteuid()
    root = PurePosixPath(home)
    if (
        not isinstance(home, str)
        or not root.is_absolute()
        or str(root) != home
        or home == "/"
        or any(part in (".", "..") for part in root.parts)
    ):
        raise ValueError("path")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    with ExitStack() as stack:
        parent = os.open("/", directory_flags)
        stack.callback(os.close, parent)
        parts = root.parts[1:]
        for index, name in enumerate((*parts, *relative[:-1])):
            try:
                parent = os.open(name, directory_flags, dir_fd=parent)
            except FileNotFoundError:
                # Missing native subdirectories are a normal first launch;
                # missing the selected private HOME/its ancestry is not.
                if index < len(parts):
                    raise ValueError("home") from None
                raise
            stack.callback(os.close, parent)
            info = os.fstat(parent)
            inside = index >= len(parts) - 1
            if info.st_uid not in ((uid,) if inside else (0, uid)):
                raise ValueError("owner")
            if inside and info.st_mode & 0o022:
                raise ValueError("mode")
        descriptor = os.open(
            relative[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=parent,
        )
        stack.callback(os.close, descriptor)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != uid
            or before.st_nlink != 1
            or before.st_mode & 0o022
            or before.st_size > limit
        ):
            raise ValueError("file")
        chunks, remaining = [], limit + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (
            remaining == 0
            or after.st_nlink != 1
            or any(getattr(before, key) != getattr(after, key) for key in fields)
        ):
            raise ValueError("changed")
        return b"".join(chunks).decode("utf-8")


def read_snapshot(home, directory, release):
    """Local/container file probe. No native CLI/API, environment read or writes.

    ``missing`` alone permits the legacy selection policy. A saved empty active
    scope is ``valid`` with no tabs. Unsafe paths, unreadable/malformed files and
    unverified releases are ``invalid``, never an excuse to reopen history.
    The calling transport supplies the standard private HOME/XDG layout.
    """
    if not supports_web_tabs(release):
        return _bad("unsupported-version")
    if not isinstance(directory, str) or not directory.startswith("/"):
        return _bad("unsafe-path")
    try:
        try:
            text = _read(home, (".config", "opencode", "cli.json"), MAX_PREFERENCES)
        except FileNotFoundError:
            text = "{}"
    except (OSError, ValueError, TypeError):
        return _bad("unsafe-path")
    try:
        preferences = _json(text, comments=True)
        if not isinstance(preferences, dict):
            raise TypeError("preferences")
        tabs = preferences.get("tabs", {})
        if not isinstance(tabs, dict):
            raise TypeError("tabs")
        scope = tabs.get("scope", "cwd")
        mode = tabs.get("mode")
        legacy = tabs.get("enabled")
        if (
            scope not in ("cwd", "global")
            or ("mode" in tabs and mode not in ("auto", "on", "off"))
            or ("enabled" in tabs and type(legacy) is not bool)
        ):
            raise ValueError("preferences")
        if mode == "off" or (mode is None and legacy is False):
            return Snapshot("missing", scope=scope, reason="disabled")
    except (ValueError, TypeError, RecursionError):
        return _bad("invalid-preferences")
    try:
        text = _read(
            home,
            (".local", "state", "opencode", "latest", "tui", "tabs.json"),
            MAX_STATE,
        )
    except FileNotFoundError:
        return Snapshot("missing", scope=scope, reason="absent")
    except (OSError, ValueError, TypeError):
        return _bad("unsafe-path")
    try:
        state = _json(text)
        if (
            not isinstance(state, dict)
            or not isinstance(state.get("global"), dict)
            or not isinstance(state.get("cwd"), dict)
            or len(state["cwd"]) > MAX_TABS
        ):
            raise ValueError("state")
        if scope == "cwd" and directory not in state["cwd"]:
            return Snapshot("missing", scope=scope, reason="scope-absent")
        active = state["global"] if scope == "global" else state["cwd"][directory]
        if (
            not isinstance(active, dict)
            or not isinstance(active.get("tabs"), list)
            or len(active["tabs"]) > MAX_TABS
            or not isinstance(active.get("unread"), dict)
        ):
            raise ValueError("scope")
        identities = []
        for tab in active["tabs"]:
            if not isinstance(tab, dict) or not _id(tab.get("sessionID")):
                raise ValueError("tab")
            if "title" in tab and not isinstance(tab["title"], str):
                raise ValueError("title")
            identities.append(tab["sessionID"])
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate")
        return Snapshot("valid", tuple(identities), scope)
    except (ValueError, TypeError, RecursionError):
        return _bad("invalid-state")


def _decode(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"status", "tabs", "scope", "reason"}
        or value["status"] not in STATUSES
        or value["scope"] not in ("cwd", "global")
        or not isinstance(value["tabs"], list)
        or len(value["tabs"]) > MAX_TABS
        or any(not _id(ident) for ident in value["tabs"])
        or len(set(value["tabs"])) != len(value["tabs"])
        or (value["reason"] is not None and value["reason"] not in REASONS)
        or (value["status"] != "valid" and value["tabs"])
        or (value["status"] == "valid" and value["reason"] is not None)
        or (value["status"] != "valid" and value["reason"] is None)
        or (value["status"] == "missing" and value["reason"] not in MISSING_REASONS)
        or (value["status"] == "invalid" and value["reason"] in MISSING_REASONS)
    ):
        raise ValueError("reply")
    return Snapshot(
        value["status"], tuple(value["tabs"]), value["scope"], value["reason"]
    )


def inspect(box, data, *, connection, timeout=2):
    """Probe through the existing providerless client transport, never a service.

    ``connection`` is mandatory: Owner.handle threads do not inherit CONNECTION.
    This function never calls list_container_sessions, run_v2_history, native
    discovery, credential refresh or a native executable. Inventory is supplied
    separately to choose/revalidate by a caller bound to its existing source.
    """
    release = data.get("opencode_version")
    if isinstance(release, str) and release.startswith("1."):
        return Snapshot("missing", reason="legacy")
    if not supports_web_tabs(release):
        return _bad("unsupported-version")
    if (
        type(timeout) not in (int, float)
        or not 0 < timeout <= 30
        or not math.isfinite(timeout)
    ):
        return _bad("transport")
    try:
        if (
            not isinstance(connection, dict)
            or connection.get("workspace") != data["id"]
        ):
            return _bad("transport")
        box.workspace_service.validate_endpoint(connection)
        command = box.workspace_service.client_command(
            box,
            data,
            connection,
            [
                "python3",
                "-I",
                "-S",
                "-B",
                "-c",
                Path(__file__).read_text(),
                json.dumps(
                    {
                        "home": data["home"],
                        "directory": box.container_project(data),
                        "release": release,
                    }
                ),
            ],
        )
        result = box.run(
            command, capture=True, private=True, check=False, timeout=timeout
        )
        if result.returncode or len(result.stdout) > MAX_REPLY:
            return _bad("transport")
        return _decode(_json(result.stdout))
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
        subprocess.SubprocessError,
        box.BoxError,
    ):
        return _bad("transport")


def _time(value):
    # Native JavaScript timestamps cannot represent integers beyond this range.
    return type(value) in (int, float) and 0 <= value <= 2**53 - 1


def _roots(inventory, directory):
    if not isinstance(inventory, list):
        raise TabError("Native tab selection refused (invalid-inventory).")
    eligible, seen = set(), set()
    for info in inventory:
        if (
            not isinstance(info, dict)
            or not _id(info.get("id"))
            or info["id"] in seen
            or not isinstance(info.get("location"), dict)
            or not isinstance(info["location"].get("directory"), str)
            or not isinstance(info.get("time"), dict)
            or not _time(info["time"].get("updated"))
            or (
                info["time"].get("archived") is not None
                and not _time(info["time"]["archived"])
            )
            or (info.get("parentID") is not None and not _id(info["parentID"]))
        ):
            raise TabError("Native tab selection refused (invalid-inventory).")
        seen.add(info["id"])
        if (
            info["location"]["directory"] == directory
            and not info.get("parentID")
            and info["time"].get("archived") is None
        ):
            eligible.add(info["id"])
    return eligible


def choose(snapshot, inventory, last, *, directory):
    """Select a startup root without changing inventory, history or native state.

    A missing snapshot permits the verified legacy ``last``. For valid native
    state, retain it only if open, otherwise choose the first eligible open tab.
    Valid empty state returns None and MUST NOT trigger automatic creation.
    Invalid state raises a fixed refusal, never legacy fallback.
    """
    try:
        if not isinstance(snapshot, Snapshot):
            raise TypeError("snapshot")
        snapshot = _decode({**asdict(snapshot), "tabs": list(snapshot.tabs)})
        if snapshot.status == "invalid":
            raise ValueError("snapshot")
    except (ValueError, TypeError):
        raise TabError("Native tab selection refused (invalid-snapshot).")
    eligible = _roots(inventory, directory)
    last = last if _id(last) else None
    if snapshot.status == "missing":
        return last if last in eligible else None
    opened = [ident for ident in snapshot.tabs if ident in eligible]
    return last if last in opened else next(iter(opened), None)


def revalidate(snapshot, inventory, current, *, directory):
    """Admission-time default recheck; no guess about active UI focus.

    Can rebind to another verified open root when the prior default was closed.
    Invalid state refuses binding (None). Missing state retains only a verified
    legacy current default, never chooses a different historical session.
    Callers must acquire/hold their ordinary client lease across metadata I/O.
    """
    if not isinstance(snapshot, Snapshot) or snapshot.status == "invalid":
        return None
    return choose(snapshot, inventory, current, directory=directory)


if __name__ == "__main__":
    try:
        answer = read_snapshot(**json.loads(sys.argv[1]))
    except (OSError, ValueError, TypeError, KeyError, RecursionError, IndexError):
        answer = _bad("transport")
    print(json.dumps(asdict(answer), separators=(",", ":")))
