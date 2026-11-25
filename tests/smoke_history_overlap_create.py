"""Verify one-shot native create-only semantics for the exact stock V2 builds."""

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
from urllib.error import HTTPError

from oc_box import oc_box, opencode_v2_io
from smoke_history_overlap_archive import _STOCK, require
from smoke_history_source_v24 import native
from smoke_session_sync_v2 import seed


def exercise(binary, version):
    with tempfile.TemporaryDirectory(prefix="history-overlap-http-create-") as name:
        root = Path(name)
        source, host, project = root / "source", root / "host", root / "project"
        for directory in (source, host, project):
            directory.mkdir(mode=0o700)

        with native(binary, source, version) as request:
            original = seed(request, "ses_once_native_create", project)
            export = opencode_v2_io.checked_export(request, original)
        archived = oc_box.archive_session(
            export, {"id": "a" * 32, "project": str(project)}
        )
        payload = {
            "info": archived["info"],
            "messages": archived["messages"],
            "location": {"directory": str(project)},
        }
        ident = payload["info"]["id"]

        def create(body):
            # One request per writer; verify its cleanup before the next
            # native operation. No shared service, delete, or retry.
            with native(binary, host, version) as destination:
                try:
                    accepted = destination(
                        "POST", "/api/experimental/session/import", body
                    )
                    return 200, accepted.get("data", {}).get("id")
                except HTTPError as error:
                    error.close()
                    return error.code, None

        def checked(key):
            with native(binary, host, version) as verifier:
                return opencode_v2_io.checked_export(verifier, key)

        require(create(payload) == (200, ident), "fresh target not acknowledged")
        previous = checked(ident)
        require(
            previous["messages"] == payload["messages"],
            "first native readback differed",
        )
        changed = {
            **payload,
            "info": {**payload["info"], "title": "Synthetic conflicting content"},
            "messages": [{**payload["messages"][0], "text": "not the original"}],
        }
        require(create(changed) == (409, None), "existing target did not conflict")
        other = "ses_other_with_same_message"
        require(
            create({**changed, "info": {**changed["info"], "id": other}})
            == (500, None),
            "foreign message collision not refused",
        )
        try:
            checked(other)
        except HTTPError as error:
            error.close()
            require(error.code == 404, "failed to verify the rejected target")
        else:
            raise RuntimeError("foreign message collision partially created a session")
        require(checked(ident) == previous, "conflict changed the existing session")
    return {
        "release": version,
        "fresh_create_verified": True,
        "existing_target_conflict": 409,
        "message_collision_refused_without_partial_session": True,
        "prior_archive_preserved": True,
        "one_request_per_fresh_writer": True,
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
