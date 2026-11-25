"""Construct a closed host-archive environment without selecting another store."""

import json
import os
from pathlib import Path

if __package__:
    from .history_overlap_order import OverlapRefusal
else:
    from history_overlap_order import OverlapRefusal


_RELEASES = frozenset(("2.4.0", "2.4.1"))
_STORAGE_SELECTORS = frozenset(("OPENCODE_DB", "OPENCODE_DISABLE_CHANNEL_DB"))


def _absolute(value):
    if (
        type(value) is not str
        or not 1 <= len(value) <= 4096
        or value.startswith("//")
        or not value.startswith("/")
        or value != os.path.normpath(value)
        or any(ord(char) < 32 for char in value)
    ):
        raise OverlapRefusal("host-environment")
    return Path(value)


def private_host_archive_env(release, ordinary, *, scratch):
    """Keep the ordinary data selector, isolate all *configured* host side roots.

    The caller pins and verifies the actual native data/store and every
    backing root. This is a closed environment constructor, not a path or
    writable-domain proof. It neither creates scratch nor runs native code.
    """
    if type(release) is not str or release not in _RELEASES:
        raise OverlapRefusal("host-version")
    if not isinstance(ordinary, dict) or _STORAGE_SELECTORS.intersection(ordinary):
        raise OverlapRefusal("host-database-selector")
    data = _absolute(ordinary.get("XDG_DATA_HOME"))
    root = _absolute(scratch)
    if data.is_relative_to(root) or root.is_relative_to(data):
        raise OverlapRefusal("host-environment")
    home = str(root / "home")
    return {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "HOME": home,
        "OPENCODE_TEST_HOME": home,
        "XDG_DATA_HOME": str(data),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_STATE_HOME": str(root / "state"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_RUNTIME_DIR": str(root / "runtime"),
        "TMPDIR": str(root / "tmp"),
        "OPENCODE_CONFIG_PROJECT_DISABLE": "1",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_CONFIG_CONTENT": json.dumps(
            {
                "snapshots": False,
                "formatter": False,
                "update": "disable",
                "warming": False,
                "automations": False,
                "policies": [
                    {"action": action, "effect": "deny", "resource": "*"}
                    for action in ("plugin.use", "provider.use", "tool.use")
                ],
            }
        ),
    }
