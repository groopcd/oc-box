"""Verify sealed candidate import/readback on disposable native 2.4 stores."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

from oc_box import history_overlap_archive, oc_box, opencode_compat
from oc_box.history_overlap_transaction import native_missing_target
from smoke_history_source_v24 import native
from smoke_session_sync_v2 import private_environment, seed


_STOCK = {
    "2.4.0": "7915c1cf09fac2fa475dc5f2e008486579da1c181a439699e2097ac3866f1da3",
    "2.4.1": "ec93043f18f8a6b4d8e71ec9260e292ba6f91b66b142782135d40f0b58b0c152",
}


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def exercise(binary, version):
    with tempfile.TemporaryDirectory(prefix="history-overlap-archive-") as directory:
        root = Path(directory)
        project, source = root / "synthetic-project", root / "source"
        project.mkdir(mode=0o700)
        source.mkdir(mode=0o700)
        with native(binary, source, version) as request:
            ident = seed(request, "ses_sealed_candidate", project)
            export = request("GET", f"/api/experimental/session/{ident}/export")["data"]
        data = {"id": "a" * 32, "project": str(project), "opencode_version": version}
        workspace = root / "registry" / "workspace.json"
        controller = workspace.parent / "controller"
        controller.mkdir(parents=True, mode=0o700)
        canonical = workspace.parent / "archive.json"
        archived = oc_box.archive_session(export, data)
        env = private_environment(root / "destination")

        def host(_data, *args):
            return opencode_compat.command(version, binary, args)

        def import_candidate(candidate):
            return oc_box.run(
                host(data, "--pure", "import", candidate.native_path()),
                cwd=controller,
                env=env,
                capture=True,
                private=True,
                stdin=subprocess.DEVNULL,
                timeout=30,
            )

        def absent(archive_id):
            observed = oc_box.run(
                host(data, "api", "get", "/api/session/" + archive_id),
                cwd=controller,
                env=env,
                capture=True,
                private=True,
                check=False,
                stdin=subprocess.DEVNULL,
                timeout=30,
            )
            return native_missing_target(observed, archive_id)

        with (
            patch.object(oc_box, "host_command", side_effect=host),
            patch.object(oc_box, "private_host_env", return_value=env),
        ):
            archive_id = archived["info"]["id"]
            require(absent(archive_id), "missing native target not verified")
            with history_overlap_archive.sealed_candidate(
                oc_box._json_byte_chunks(archived)
            ) as first:
                require(
                    import_candidate(first).returncode == 0, "native import refused"
                )
                require(
                    not canonical.exists(), "canonical archive written before readback"
                )
                oc_box.verify_host_archive(workspace, data, archived)
                with oc_box._atomic_json_file(
                    canonical, binary=True, durable=True
                ) as output:
                    first.write_to(output)
                require(
                    hashlib.sha256(canonical.read_bytes()).hexdigest() == first.digest,
                    "published bytes differed from sealed candidate",
                )
            require(not absent(archive_id), "existing native target accepted as absent")
            previous = canonical.read_bytes()
            changed = {
                **archived,
                "info": {**archived["info"], "title": "Synthetic changed title"},
            }
            # Native import can return zero despite conflict. Do not retry
            # or publish a canonical archive just because the command exited.
            with history_overlap_archive.sealed_candidate(
                oc_box._json_byte_chunks(changed)
            ) as replacement:
                require(
                    import_candidate(replacement).returncode == 0,
                    "expected conflict no-op",
                )
                try:
                    oc_box.verify_host_archive(workspace, data, changed)
                except oc_box.BoxError:
                    pass
                else:
                    raise RuntimeError(
                        "unverified no-op accepted as the changed candidate"
                    )
            oc_box.verify_host_archive(workspace, data, archived)
            require(canonical.read_bytes() == previous, "prior private archive changed")
    return {
        "release": version,
        "sealed_import_verified": True,
        "canonical_published_after_readback": True,
        "conflict_zero_not_success": True,
        "prior_archive_preserved": True,
        "native_absence_bound_to_target": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", type=Path, required=True)
    parser.add_argument("--release", choices=tuple(_STOCK), required=True)
    args = parser.parse_args()
    binary = args.opencode.resolve(strict=True)
    with binary.open("rb") as stream:
        # Local native fixtures do not authorize a modified executable or shim.
        hasher = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    require(hasher.hexdigest() == _STOCK[args.release], "unexpected native binary")
    print(json.dumps(exercise(str(binary), args.release), sort_keys=True))


if __name__ == "__main__":
    main()
