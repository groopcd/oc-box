"""Join native reads, fresh native creates and real durable local bookkeeping."""

import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from unittest.mock import patch

from oc_box import (
    history_overlap_archive,
    history_overlap_writer,
    history_write_intent,
    oc_box,
    opencode_v2_io,
    runtime_safeguard,
    workspace_control,
    workspace_service,
)
from oc_box.history_overlap_transaction import DestinationCreate, FirstPass
from smoke_history_overlap_archive import _STOCK, require
from smoke_history_source_v24 import native
from smoke_session_sync_v2 import private_environment, seed


IDS = ("ses_overlap_a", "ses_overlap_b", "ses_overlap_c")


def exercise(binary, version):
    with (
        tempfile.TemporaryDirectory(prefix="history-overlap-real-loop-") as name,
        open(binary, "rb") as native_file,
    ):
        fd = native_file.fileno()
        identity = runtime_safeguard._signature(os.fstat(fd))
        digest = hashlib.sha256()
        for chunk in iter(lambda: native_file.read(1024 * 1024), b""):
            digest.update(chunk)
        require(
            identity == runtime_safeguard._signature(os.fstat(fd)),
            "native executable changed",
        )
        require(digest.hexdigest() == _STOCK[version], "unexpected native binary")
        pins = runtime_safeguard.HostBinary(fd, identity, digest.hexdigest())
        root = Path(name)
        project, source = root / "synthetic-project", root / "source"
        project.mkdir(mode=0o700)
        source.mkdir(mode=0o700)
        data = {
            "id": "a" * 32,
            "project": str(project),
            "container": "b" * 64,
            "opencode_version": version,
        }
        workspace = root / data["id"] / "workspace.json"
        controller = workspace.parent / "controller"
        controller.mkdir(parents=True, mode=0o700)
        oc_box.atomic_json(workspace, data)
        ordinary = private_environment(root / "ordinary")
        del ordinary["OPENCODE_DB"]
        scratch = root / "writer-scratch"
        scratch.mkdir(mode=0o700)
        scoped = history_overlap_writer.writer_environment(
            scratch, ordinary["XDG_DATA_HOME"], version
        )
        baseline = workspace_control.capture(workspace)

        @contextlib.contextmanager
        def admitted():
            with workspace_control.admission(workspace, timeout=1):
                require(
                    workspace_control.unchanged(workspace, baseline),
                    "synthetic stop intent",
                )
                yield

        def create(candidate, target):
            return history_overlap_writer.create_once(
                pins,
                version,
                scoped,
                controller,
                candidate,
                target,
                gate=admitted,
                deadline=time.monotonic() + 15,
            )

        def readback(archive_id):
            result = subprocess.run(
                [binary, "session", "export", archive_id, "--standalone"],
                cwd=controller,
                env=ordinary,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=30,
                check=True,
            )
            return json.loads(result.stdout)

        def destination(path, _data, route, archived, target):
            def from_sealed(sealed, selected):
                require(
                    selected == route and not target.exists(),
                    "unexpected prior private archive",
                )
                candidate = json.loads(Path(sealed.native_path()).read_bytes())
                require(candidate == archived, "sealed archive changed")
                return create(sealed, route.archive)

            return DestinationCreate(
                from_sealed,
                lambda selected: history_overlap_writer.readback_once(
                    pins,
                    version,
                    scoped,
                    controller,
                    selected.archive,
                    gate=admitted,
                    deadline=time.monotonic() + 15,
                ),
                admitted,
            )

        with native(binary, source, version) as request:
            for ident in IDS:
                seed(request, ident, project)

            def checked(ident):
                return opencode_v2_io.checked_export(request, ident)

            colliding = oc_box.archive_session(checked(IDS[1]), data)
            colliding["info"]["title"] = "Pre-existing unrelated collision"
            with history_overlap_archive.sealed_candidate(
                oc_box._json_byte_chunks(colliding)
            ) as sealed:
                require(
                    create(sealed, colliding["info"]["id"]) == colliding["info"]["id"],
                    "could not seed conflict",
                )
            original = readback(colliding["info"]["id"])

            cancelled = []
            written = []

            def record_destination(*args):
                selected = destination(*args)

                def collect(source_path, route):
                    written.append(route.source)
                    return selected.create(source_path, route)

                return DestinationCreate(
                    collect, selected.readback, selected.publish_gate
                )

            overlap = FirstPass(
                lambda *_: {
                    "sessions": [
                        row["id"]
                        for row in opencode_v2_io.session_inventory(
                            request, str(project)
                        )
                    ]
                },
                lambda route: checked(route.source),
                lambda route: route.source,
                lambda route, _handle: checked(route.source),
                lambda route, _handle: cancelled.append(route.source) is None,
                record_destination,
            )
            binding = oc_box._QuietSyncBinding(data["container"], (), {})
            binding.start_verified = True
            token = oc_box._QUIET_SYNC_BINDING.set(binding)
            try:
                with (
                    oc_box.workspace_lock(workspace),
                    patch.object(
                        workspace_service,
                        "live_connection",
                        return_value={"workspace": data["id"]},
                    ),
                    patch.object(
                        workspace_service, "quiet_history_source", return_value=object()
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    try:
                        oc_box.sync_sessions(workspace, data, first_overlap=overlap)
                    except history_overlap_writer.WriterRefusal as error:
                        require(
                            error.category == "conflict"
                            and not error.history_destination_cleanup_failed,
                            "unexpected guarded failure",
                        )
                    else:
                        raise RuntimeError("native conflict returned success")
                    try:
                        oc_box.sync_sessions(workspace, data)
                    except oc_box.BoxError as error:
                        require(
                            "is unresolved" in str(error),
                            "serial fence did not see the attempt",
                        )
                    else:
                        raise RuntimeError("serial sync bypassed the attempt")
            finally:
                oc_box._QUIET_SYNC_BINDING.reset(token)

        state = oc_box.session_state(workspace)
        require(set(state["synced"]) == {IDS[0]}, "unexpected checkpoint")
        require(written == list(IDS[:2]) and cancelled == [IDS[2]], "work was replayed")
        require(
            readback(colliding["info"]["id"]) == original,
            "conflict changed pre-existing history",
        )
        with history_write_intent.Journal(
            workspace, data["id"], profile=oc_box._overlap_journal_profile(data)
        ) as journal:
            attempt = journal.load()
            require(
                attempt.source_id == IDS[1] and attempt.atomic_create, "lost intent"
            )
    return {
        "release": version,
        "verified_prefix": 1,
        "unresolved_conflict": 1,
        "later_source_cancelled_without_host_write": True,
        "serial_retry_refused": True,
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
