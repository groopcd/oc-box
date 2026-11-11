"""Private provider-free native session backups; not host/Podman acceptance.

Run with uv run --no-project --with-editable . python -B
tests/smoke_session_backups_v2.py --opencode /path/to/unmodified-opencode-v2.
Only synthetic history and an ephemeral age identity are used. No plaintext
transcript files, normal service discovery, workspace starts or inference.
"""

import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

from oc_box import backup_crypto as crypto
from oc_box import opencode_v2_io as bridge
from oc_box import session_backups as engine
from oc_box import tui_sessions


def require(condition):
    if not condition:
        raise RuntimeError("Native session backup fixture assertion failed.")


def environment(root):
    result = {
        "PATH": os.environ["PATH"],
        "LANG": "C.UTF-8",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "OPENCODE_DB": "session-backup-fixture.db",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_CONFIG_CONTENT": json.dumps(
            {
                "snapshots": False,
                "formatter": False,
                "update": "disable",
                "plugins": [],
                "policies": [
                    {"action": "plugin.use", "resource": "*", "effect": "deny"}
                ],
            }
        ),
    }
    for key, name in (
        ("HOME", "home"),
        ("TMPDIR", "tmp"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_STATE_HOME", "state"),
    ):
        location = root / name
        location.mkdir(mode=0o700)
        result[key] = str(location)
    result["OPENCODE_TEST_HOME"] = result["HOME"]
    return result


def seed(request, ident, directory, *, parent=None, large=False):
    info = {
        "id": ident,
        "projectID": "global",
        "location": {"directory": str(directory)},
        "title": "Synthetic native backup fixture",
        "time": {"created": 1, "updated": 5},
        "cost": 0,
        "tokens": {
            "input": 0,
            "output": 0,
            "reasoning": 0,
            "cache": {"read": 0, "write": 0},
        },
        "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
        "metadata": {"untrusted": {"auth": "SYNTHETIC_ONLY"}},
    }
    if parent:
        info["parentID"] = parent
    messages = [
        {
            "id": "msg_" + ident[4:] + "_user",
            "type": "user",
            "time": {"created": 1},
            "text": "SYNTHETIC_HISTORY_ONLY"
            + ("x" * (crypto.MAX_SMALL + 10) if large else ""),
        }
    ]
    if large:
        messages.extend(
            [
                {
                    "id": "msg_" + ident[4:] + "_assistant",
                    "type": "assistant",
                    "agent": "build",
                    "model": {"providerID": "fixture", "id": "fixture"},
                    "time": {"created": 2, "completed": 3},
                    "content": [{"type": "text", "text": "Synthetic completed reply"}],
                },
                {
                    "id": "msg_" + ident[4:] + "_compaction",
                    "type": "compaction",
                    "status": "completed",
                    "reason": "manual",
                    "summary": "Synthetic summary",
                    "time": {"created": 4},
                    "metadata": {"keepID": messages[0]["id"]},
                },
            ]
        )
    request(
        "POST",
        "/api/experimental/session/import",
        {"info": info, "messages": messages, "location": {"directory": str(directory)}},
    )


def run(binary):
    binary = str(Path(binary).absolute())
    with tempfile.TemporaryDirectory(
        prefix="oc-box-native-session-backup-"
    ) as temporary:
        root = Path(temporary)
        source, target = root / "source", root / "target"
        source.mkdir(mode=0o700)
        target.mkdir(mode=0o700)
        env = environment(root)
        with patch.dict(os.environ, env, clear=True):
            version = subprocess.run(
                [binary, "--version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            require(version.returncode == 0 and b"2.2.6" in version.stdout)
            private = crypto.small(["age-keygen"])
            recipient = (
                crypto.small(["age-keygen", "-y"], data=private).decode().strip()
            )
            identity = root / "synthetic-age-identity"
            identity.write_bytes(private)
            identity.chmod(0o600)
            with bridge.server(binary) as request:
                seed(request, "ses_fixture_foreign", source)
                seed(
                    request,
                    "ses_fixture_root",
                    source,
                    parent="ses_fixture_foreign",
                    large=True,
                )
                seed(request, "ses_fixture_child", source, parent="ses_fixture_root")
                catalog = tui_sessions.SessionCatalog(
                    request,
                    "host",
                    "private-synthetic-fixture",
                    ownership=lambda *_: "exclusive",
                )
                original = {
                    item["id"]: copy.deepcopy(
                        bridge.checked_export(request, item["id"])
                    )
                    for item in catalog.list()
                }
                output = root / "synthetic.ocsession"
                review = engine.backup(
                    catalog, "ses_fixture_root", output, recipients=[recipient]
                )
                require(review["session_count"] == 2 and review["message_count"] == 4)
                require(b"SYNTHETIC_HISTORY_ONLY" not in output.read_bytes())
                require(
                    "SYNTHETIC_HISTORY_ONLY"
                    not in json.dumps(engine.inspect(output, identities=[identity]))
                )
                plan = engine.prepare_restore(
                    catalog, output, str(target), identities=[identity]
                )
                expected = copy.deepcopy(plan._payload)
                result = engine.restore(catalog, plan)
                require(len(result["created_ids"]) == 2 and result["atomic"] is False)
                created = {
                    ident: bridge.checked_export(request, ident)
                    for ident in result["created_ids"]
                }
                for item in expected:
                    observed = created[item["info"]["id"]]
                    require(observed["messages"] == item["messages"])
                    require(observed["info"]["permissions"] == [])
                    require(observed["info"]["location"] == {"directory": str(target)})
                restored_root, restored_child = [
                    created[ident] for ident in result["created_ids"]
                ]
                require("parentID" not in restored_root["info"])
                require(
                    restored_child["info"]["parentID"] == restored_root["info"]["id"]
                )
                for ident, exported in original.items():
                    require(bridge.checked_export(request, ident) == exported)
                require(len(catalog.list()) == len(original) + 2)
                require(not any(path.suffix == ".json" for path in root.iterdir()))
    print(
        "Native 2.2.6 encrypted session-tree backup/restore passed (private, provider-free; no host acceptance)."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True)
    args = parser.parse_args()
    try:
        run(args.opencode)
    except engine.SessionBackupError as error:
        raise SystemExit(
            "Native session backup fixture failed (" + error.reason + ")."
        ) from None
    except Exception:
        raise SystemExit(
            "Native session backup fixture failed; private output was not logged."
        ) from None


if __name__ == "__main__":
    main()
