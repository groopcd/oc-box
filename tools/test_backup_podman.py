#!/usr/bin/env python3
"""Bounded opt-in live rootless Podman backup/restore test on a HOST terminal.

Creates ONLY randomly labeled disposable fixtures in an isolated registry. No
OpenCode/provider credentials, real project, or vault is used. On failure it
retains fixtures and prints their paths/IDs for diagnosis; --cleanup removes
only resources with the fixture IDs recorded by this test.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "src"))
from oc_box import backups, backup_crypto as crypto, oc_box as box


def run(root, image):
    original = os.environ.get("OC_BOX_STATE_HOME")
    os.environ["OC_BOX_STATE_HOME"] = str(root / "registry")
    ids = []
    try:
        box.check_backend(env=crypto.environment())
        crypto.tools()
        identity = root / "identity.txt"
        identity.write_bytes(crypto.small(["age-keygen"]))
        identity.chmod(0o600)
        recipient = crypto.small(["age-keygen", "-y", str(identity)]).decode().strip()
        source = root / "source"
        source.mkdir()
        path, data = box.create_workspace(source, "alpine")
        box.check_resource_controllers(data)
        data["image"] = image
        data["ready"] = True  # fixture: no full OpenCode/runtime install required
        box.atomic_json(path, data)
        ids.append(data["id"])
        (root / "fixture-ids.json").write_text(json.dumps(ids))
        (source / "unfinished.txt").write_text("uncommitted source sentinel\n")
        (source / ".git").mkdir()
        (source / ".git/HEAD").write_text("ref: refs/heads/master\n")
        box.atomic_json(
            box.session_file(path), {"sessions": ["ses_fixture"], "last": "ses_fixture"}
        )
        box.create_container(path, data, environment=crypto.environment())
        data["ready"] = True  # create_container normally marks new OS unprepared
        box.atomic_json(path, data)
        backups.command(box, "start", data["container"])
        home_seed = root / "seed-home"
        database = home_seed / ".local/share/opencode/opencode.db"
        database.parent.mkdir(parents=True)
        with sqlite3.connect(database) as connection:
            connection.execute("create table fixture(id text, content text)")
            connection.execute(
                "insert into fixture values('ses_fixture','saved conversation sentinel')"
            )
        (home_seed / ".config").mkdir()
        (home_seed / ".config/synthetic-auth").write_text(
            "synthetic credential sentinel\n"
        )
        backups.command(
            box, "cp", str(home_seed) + "/.", data["container"] + ":" + data["home"]
        )
        backups.command(
            box,
            "exec",
            data["container"],
            "/bin/sh",
            "-c",
            'printf \'installed OS sentinel\\n\' > /etc/oc-box-backup-fixture; mkdir -p /root-owned; printf \'system file\\n\' > /root-owned/sentinel; chown -R 0:0 "$HOME/.local" "$HOME/.config"; printf \'mapped owner sentinel\\n\' > "$HOME/mapped-owner"; chown 1:1 "$HOME/mapped-owner"',
        )
        source_db_digest = hashlib.sha256(database.read_bytes()).hexdigest()
        for portable in (False, True):
            kind = "portable" if portable else "full"
            output = root / (kind + ".ocbox")
            backups.make_backup(
                box,
                argparse.Namespace(
                    project=source,
                    workspace=None,
                    output=output,
                    portable=portable,
                    recipient=[recipient],
                ),
            )
            destination = root / ("restored-" + kind)
            backups.restore_backup(
                box,
                argparse.Namespace(
                    project=destination, file=output, identity=[identity]
                ),
            )
            restored_path, restored = box.find_workspace(destination)
            assert restored_path is not None and restored is not None
            ids.append(restored["id"])
            (root / "fixture-ids.json").write_text(json.dumps(ids))
            assert (
                destination / "unfinished.txt"
            ).read_text() == "uncommitted source sentinel\n"
            assert box.session_state(restored_path)["last"] == "ses_fixture"
            info = backups.owned_container(box, restored)
            assert info is not None and not info["State"]["Running"]
            volume = backups.owned_volume(box, restored)
            mapped_ownership = (
                crypto.small(
                    [
                        "podman",
                        "unshare",
                        sys.executable,
                        "-c",
                        "import os,sys; p=os.stat(sys.argv[1]); print(str(p.st_uid)+':'+str(p.st_gid))",
                        str(
                            Path(volume["Mountpoint"])
                            / ".local/share/opencode/opencode.db"
                        ),
                    ]
                )
                .decode()
                .strip()
            )
            assert mapped_ownership == "0:0", mapped_ownership
            other_owner = (
                crypto.small(
                    [
                        "podman",
                        "unshare",
                        sys.executable,
                        "-c",
                        "import os,sys; p=os.stat(sys.argv[1]); print(str(p.st_uid)+':'+str(p.st_gid))",
                        str(Path(volume["Mountpoint"]) / "mapped-owner"),
                    ]
                )
                .decode()
                .strip()
            )
            assert other_owner == "1:1", other_owner
            remote_db = Path(volume["Mountpoint"]) / ".local/share/opencode/opencode.db"
            digest_code = "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())"
            hashed = (
                crypto.small(
                    [
                        "podman",
                        "unshare",
                        sys.executable,
                        "-c",
                        digest_code,
                        str(remote_db),
                    ]
                )
                .decode()
                .strip()
            )
            assert hashed == source_db_digest
            backups.command(box, "start", restored["container"])
            if not portable:
                result = backups.command(
                    box,
                    "exec",
                    restored["container"],
                    "/bin/sh",
                    "-c",
                    'test "$(cat /etc/oc-box-backup-fixture)" = "installed OS sentinel" && test -f /root-owned/sentinel && test -f "$1/unfinished.txt"',
                    "fixture",
                    box.container_project(restored),
                )
                assert result.returncode == 0
            else:
                result = backups.command(
                    box,
                    "exec",
                    restored["container"],
                    "/bin/sh",
                    "-c",
                    "test ! -e /etc/oc-box-backup-fixture",
                )
                assert result.returncode == 0 and not restored["ready"]
            backups.command(box, "stop", "--time", "5", restored["container"])
            # Source container and files still exist and are not overwritten.
            assert (source / box.MARKER).read_text().strip() == data["id"]
            assert (
                source / "unfinished.txt"
            ).read_text() == "uncommitted source sentinel\n"
            print(
                f"PASS: live {kind} backup/restore: project/Git edits, private SQLite DB, session ID, stopped-state restore, internal paths and expected OS contents."
            )
        print(
            "PASS: live host Podman fixture checks completed. Synthetic backup archives remain at "
            + str(root)
        )
        (root / "result.json").write_text(
            json.dumps(
                {
                    "status": "passed",
                    "fixture_ids": ids,
                    "checks": [
                        "full",
                        "portable",
                        "SQLite bytes",
                        "rootless UID/GID",
                        "session ID",
                        "internal paths",
                        "OS state",
                        "source preservation",
                    ],
                },
                indent=2,
            )
            + "\n"
        )
        print(
            "Clean up this fixture only: python3 tools/test_backup_podman.py --cleanup "
            + str(root)
        )
    finally:
        if original is None:
            os.environ.pop("OC_BOX_STATE_HOME", None)
        else:
            os.environ["OC_BOX_STATE_HOME"] = original


def cleanup(root):
    if root.is_symlink():
        raise ValueError("Not a real test fixture directory")
    root = root.resolve(strict=True)
    if (
        not root.name.startswith("oc-box-live-backup-")
        or root.stat().st_uid != os.getuid()
    ):
        raise ValueError("Not a test fixture directory")
    ids = json.loads((root / "fixture-ids.json").read_text())
    if not isinstance(ids, list):
        raise ValueError("Invalid fixture ID list")
    os.environ["OC_BOX_STATE_HOME"] = str(root / "registry")
    # Include journals from an interrupted restore, not just completed restores
    # written to the fixture list. Every candidate must be inside this registry.
    ids = list(
        dict.fromkeys([*ids, *(path.parent.name for path in box.workspace_paths())])
    )
    for ident in ids:
        path, data = box.find_workspace(workspace_id=ident)
        assert path is not None and data is not None
        if not Path(data["project"]).resolve().is_relative_to(root):
            raise ValueError("Fixture resource points outside test directory")
        box.remove_resources(data)
    print(
        "Fixture-owned Podman resources removed. Backups and test files retained at "
        + str(root)
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image",
        default="docker.io/library/alpine:latest",
        help="small fixture image; default alpine",
    )
    parser.add_argument("--cleanup", type=Path)
    args = parser.parse_args()
    if os.environ.get("OC_BOX_CONTAINER") == "1" or os.geteuid() == 0:
        parser.exit(
            1,
            "Run this test from your regular HOST terminal, not sudo or inside oc-box.\n",
        )
    if args.cleanup:
        cleanup(args.cleanup)
        return
    root = Path(tempfile.mkdtemp(prefix="oc-box-live-backup-"))
    print("Isolated disposable fixture directory: " + str(root), flush=True)
    try:
        run(root, args.image)
    except Exception:
        print("Fixture retained for diagnosis: " + str(root), file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
