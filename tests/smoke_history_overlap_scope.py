"""Verify that isolated side roots retain the ordinary synthetic host store."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from unittest.mock import patch

from oc_box import oc_box, opencode_v2_io
from oc_box.history_overlap_host_env import private_host_archive_env
from smoke_history_overlap_archive import _STOCK, require
from smoke_history_source_v24 import native
from smoke_session_sync_v2 import private_environment, seed


def exercise(binary, version):
    with tempfile.TemporaryDirectory(prefix="history-overlap-host-scope-") as name:
        root = Path(name)
        project, source = root / "synthetic-project", root / "source"
        project.mkdir(mode=0o700)
        source.mkdir(mode=0o700)
        with native(binary, source, version) as request:
            ident = seed(request, "ses_host_scope_trial", project)
            export = opencode_v2_io.checked_export(request, ident)
        archived = oc_box.archive_session(
            export, {"id": "a" * 32, "project": str(project)}
        )
        workspace = root / "registry" / "workspace.json"
        controller = workspace.parent / "controller"
        controller.mkdir(parents=True, mode=0o700)

        ordinary = private_environment(root / "ordinary")
        # The real ordinary adapter strips custom DB selectors. Test the
        # native default in this disposable data home, not a redirected DB.
        del ordinary["OPENCODE_DB"]
        scratch = root / "scratch"
        scoped = private_host_archive_env(version, ordinary, scratch=str(scratch))
        for key in (
            "HOME",
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
            "XDG_CACHE_HOME",
            "XDG_RUNTIME_DIR",
            "TMPDIR",
        ):
            Path(scoped[key]).mkdir(mode=0o700, parents=True, exist_ok=True)

        def invoke(environment, *arguments):
            return subprocess.run(
                [binary, *arguments],
                cwd=controller,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=True,
                timeout=30,
            ).stdout

        ordinary_path = invoke(ordinary, "debug", "paths", "db")
        scoped_path = invoke(scoped, "debug", "paths", "db")
        expected = Path(ordinary["XDG_DATA_HOME"]) / "opencode" / "opencode.db"
        require(
            ordinary_path == scoped_path == os.fsencode(expected) + b"\n",
            "selected host path changed",
        )
        require(not expected.exists(), "selector unexpectedly created the database")

        previous_cwd = Path.cwd()
        try:
            os.chdir(controller)
            exit_info = {}
            with (
                patch.dict(os.environ, scoped, clear=True),
                opencode_v2_io.server(
                    binary,
                    exit_info=exit_info,
                    startup_deadline=time.monotonic() + 15,
                    exact_history_source=True,
                ) as writer,
            ):
                accepted = writer(
                    "POST",
                    "/api/experimental/session/import",
                    {
                        "info": archived["info"],
                        "messages": archived["messages"],
                        "location": {"directory": str(controller)},
                    },
                )
                require(
                    accepted["data"]["id"] == archived["info"]["id"],
                    "native create acknowledgement differed",
                )
            require(
                exit_info == {"code": 0, "forced": False}, "writer cleanup unverified"
            )
        finally:
            os.chdir(previous_cwd)
        require(expected.is_file(), "native write opened a different store")
        imported = json.loads(
            invoke(
                ordinary, "session", "export", archived["info"]["id"], "--standalone"
            )
        )
        oc_box._verify_host_projection(workspace, archived, imported)
    return {
        "release": version,
        "same_native_host_selector": True,
        "fresh_writer_retired": True,
        "ordinary_independent_readback_verified": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", type=Path, required=True)
    parser.add_argument("--release", choices=tuple(_STOCK), required=True)
    args = parser.parse_args()
    binary = args.opencode.resolve(strict=True)
    digest = hashlib.sha256()
    with binary.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    require(digest.hexdigest() == _STOCK[args.release], "unexpected native binary")
    print(json.dumps(exercise(str(binary), args.release), sort_keys=True))


if __name__ == "__main__":
    main()
