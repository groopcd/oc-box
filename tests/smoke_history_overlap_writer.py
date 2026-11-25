"""Exercise production create and readback on disposable native 2.4 stores."""

import argparse
import contextlib
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.error import HTTPError

from oc_box import history_overlap_archive, history_overlap_writer
from oc_box import oc_box, opencode_v2_io, runtime_safeguard
from smoke_history_overlap_archive import _STOCK, require
from smoke_history_source_v24 import native
from smoke_session_sync_v2 import seed


def exercise(binary, version):
    with tempfile.TemporaryDirectory(prefix="history-overlap-writer-") as name:
        root = Path(name)
        source, host, project, controller = (
            root / part for part in ("source", "host", "project", "controller")
        )
        for directory in (source, host, project, controller):
            directory.mkdir(mode=0o700)

        with native(binary, source, version) as request:

            def seed_large(method, route, body):
                body["messages"][0]["text"] = "Synthetic large export. " * 4096
                return request(method, route, body)

            original = seed(seed_large, "ses_once_native_writer", project)
            export = opencode_v2_io.checked_export(request, original)
        archived = oc_box.archive_session(
            export, {"id": "a" * 32, "project": str(project)}
        )
        ident = archived["info"]["id"]
        scratch = root / "writer-scratch"
        scratch.mkdir(mode=0o700)
        env = history_overlap_writer.writer_environment(
            scratch, host / ".local/share", version, database="oc-box-v2.db"
        )

        def create(body, pins):
            with history_overlap_archive.sealed_candidate(
                [json.dumps(body).encode()]
            ) as candidate:
                return history_overlap_writer.create_once(
                    pins,
                    version,
                    env,
                    controller,
                    candidate,
                    body["info"]["id"],
                    gate=contextlib.nullcontext,
                    deadline=time.monotonic() + 15,
                )

        def readback(key, pins):
            return history_overlap_writer.readback_once(
                pins,
                version,
                env,
                controller,
                key,
                gate=contextlib.nullcontext,
                deadline=time.monotonic() + 15,
            )

        def checked(key):
            with native(binary, host, version) as verifier:
                return opencode_v2_io.checked_export(verifier, key)

        with open(binary, "rb") as stream:
            fd = stream.fileno()
            identity = runtime_safeguard._signature(os.fstat(fd))
            digest = sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
            require(digest.hexdigest() == _STOCK[version], "unexpected native binary")
            require(
                runtime_safeguard._signature(os.fstat(fd)) == identity,
                "native binary changed",
            )
            pins = runtime_safeguard.HostBinary(fd, identity, digest.hexdigest())

            require(create(archived, pins) == ident, "fresh target not acknowledged")
            native_log = host / ".local/share/opencode/log/opencode.log"
            # Stock V2 writes the argv-only CLI startup notice before parsing
            # its none log level; the native server must not add wire data.
            log_lines = (
                native_log.read_bytes().splitlines() if native_log.exists() else []
            )
            require(
                len(log_lines) <= 1
                and all(
                    b'message="cli starting"' in row
                    and row.endswith(b" role=cli")
                    and b"Authorization" not in row
                    and b"ses_once_native_writer" not in row
                    for row in log_lines
                ),
                "writer emitted more than the fixed native startup notice",
            )
            previous = readback(ident, pins)
            oc_box._verify_host_projection(root / "workspace.json", archived, previous)
            require(previous == checked(ident), "independent fixture export differed")
            require(
                previous["messages"] == archived["messages"],
                "native transcript differed",
            )
            require(
                previous["info"]["location"]["directory"] == str(controller),
                "controller location differed",
            )
            changed = {
                **archived,
                "info": {**archived["info"], "title": "Synthetic conflicting content"},
                "messages": [{**archived["messages"][0], "text": "not the original"}],
            }
            try:
                create(changed, pins)
            except history_overlap_writer.WriterRefusal as error:
                require(error.category == "conflict", "same-ID conflict not proven")
                require(
                    not error.history_write_uncertain
                    and not error.history_destination_cleanup_failed,
                    "same-ID conflict retirement unproven",
                )
            else:
                raise RuntimeError("same-ID conflict acknowledged as fresh")
            other = "ses_other_with_same_message"
            try:
                create({**changed, "info": {**changed["info"], "id": other}}, pins)
            except history_overlap_writer.WriterRefusal as error:
                require(
                    error.history_write_uncertain
                    and not error.history_destination_cleanup_failed,
                    "unacknowledged mutation or writer retirement not reported",
                )
            else:
                raise RuntimeError("foreign message collision acknowledged as fresh")
            try:
                readback(other, pins)
            except history_overlap_writer.WriterRefusal as error:
                require(error.category == "readback", "missing export not refused")
                require(
                    not error.history_destination_cleanup_failed,
                    "unverified readback retirement",
                )
            else:
                raise RuntimeError("missing export returned a projection")
            try:
                checked(other)
            except HTTPError as error:
                error.close()
                require(error.code == 404, "failed to verify the rejected target")
            else:
                raise RuntimeError("message collision partially created a session")
            require(
                readback(ident, pins) == previous,
                "conflict changed the existing session",
            )
    return {
        "version": version,
        "production_writer": True,
        "independent_readback": True,
        "readback_large_projection_verified": True,
        "existing_target_preserved": True,
        "collision_left_no_partial_target": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", type=Path, required=True)
    parser.add_argument("--release", choices=tuple(_STOCK), required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            exercise(str(args.opencode.resolve(strict=True)), args.release),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
