"""Provider-free named Studio recent catalog against isolated OpenCode 2.4.1."""

import argparse
from functools import partial
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from time import monotonic
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from smoke_session_sync_v2 import bounded, private_environment, require, seed
from smoke_web_named_native import _CONTAINER, local_pair_exec
from test_web_named_studio_catalog import admitted_app, request

from oc_box import (
    oc_box as box,
    opencode_compat,
    opencode_v2_io,
    web_local_origins,
    web_named_owner,
    web_named_relay,
    web_named_studio_catalog as catalog,
    workspace_service,
)


def exercise(binary, root):
    repo = root / "repository"
    project, sibling = repo / "workspace with space", repo / "sibling"
    project.mkdir(parents=True)
    sibling.mkdir()
    env = private_environment(root / "native")
    bounded(["git", "init", "-q"], env, repo)
    bounded(
        [
            "git",
            "-c",
            "user.name=Catalog Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "Fixture",
        ],
        env,
        repo,
    )
    release = opencode_compat.version(bounded([binary, "--version"], env, project))
    require(release == "2.4.1", "Catalog smoke requires exact native 2.4.1.")
    previous = Path.cwd()
    os.chdir(project)
    exit_info = {}
    try:
        with (
            patch.dict(os.environ, env, clear=True),
            opencode_v2_io.server(binary, exit_info=exit_info) as native,
        ):
            exercise_catalog(native, env, project, sibling)
    finally:
        os.chdir(previous)
    require(
        exit_info == {"code": 0, "forced": False}, "Native smoke did not stop cleanly."
    )


def exercise_catalog(native, env, project, sibling):
    def imported(ident, directory, index, **options):
        def inject(method, path, body):
            body["info"]["time"] = {"created": index, "updated": index + 10}
            if ident == "ses_catalog_archived":
                body["info"]["time"]["archived"] = index + 20
            return native(method, path, body)

        return seed(inject, ident, directory, **options)

    require(native("GET", "/api/credential") == {"data": []}, "Unexpected credentials.")
    for i in range(catalog.RECENT_LIMIT + 1):
        imported(f"ses_catalog_{i:03}", project, i + 1)
    archived = imported("ses_catalog_archived", project, 100, title="Synthetic archive")
    child = imported(
        "ses_catalog_child", project, 101, parent=archived, title="Synthetic child"
    )
    foreign = imported("ses_catalog_foreign", sibling, 102, title="Excluded sibling")
    location = native("GET", "/api/location")
    foreign_info = native("GET", "/api/session/" + foreign)["data"]
    require(
        foreign_info["projectID"] == location["project"]["id"]
        and foreign_info["location"] != {"directory": str(project)},
        "Fixture did not establish the same-project wrong-directory case.",
    )
    query = catalog._list_path(str(project))
    baseline = native("GET", query)
    expected = [
        {
            "id": item["id"],
            "title": item["title"],
            "archived": "archived" in item["time"],
            "child": "parentID" in item,
        }
        for item in baseline["data"]
    ]
    require(
        len(expected) == catalog.RECENT_LIMIT
        and baseline["cursor"]["next"] is not None
        and {
            "id": archived,
            "title": "Synthetic archive",
            "archived": True,
            "child": False,
        }
        in expected
        and {"id": child, "title": "Synthetic child", "archived": False, "child": True}
        in expected
        and all(row["id"] != foreign for row in expected),
        "Native fixture did not establish a truncated, scoped page with child and archive.",
    )

    identity = web_named_owner.OwnerIdentity(
        "a" * 32, "b" * 32, _CONTAINER, "2.4.1", native.endpoint["url"]
    )
    origins = web_local_origins.LocalOrigins()
    control, peer = socket.socketpair()
    with control, peer:
        lease = web_named_owner.VerifiedOwnerLease(
            identity,
            control,
            str(project),
            env["HOME"],
            native.endpoint["password"],
            observe=lambda: identity,
            connect_native=partial(web_named_owner.connect_workspace_native, box),
        )
        owner = web_named_owner.NamedOwnerBinding(box, origins=origins, lease=lease)
        try:
            with local_pair_exec(project, lease):
                cookie = owner._pair_native(owner.binding, origins.workspace)
            app, token = admitted_app(owner, origins, cookie)
            actual_popen = subprocess.Popen
            port = identity.endpoint.rsplit(":", 1)[1]
            started = []

            def local_relay(command, **options):
                require(
                    command
                    == [
                        "podman",
                        "exec",
                        "-i",
                        "--workdir",
                        "/",
                        _CONTAINER,
                        "/usr/bin/env",
                        "-i",
                        "PATH=/usr/bin:/bin",
                        "python3",
                        "-I",
                        "-S",
                        "-B",
                        "-c",
                        workspace_service.PROXY,
                        port,
                    ]
                    and options["stdin"] is options["stdout"]
                    and options["stderr"] == subprocess.DEVNULL
                    and "OPENCODE_PASSWORD" not in options["env"],
                    "Catalog did not use the exact providerless managed relay.",
                )
                child_process = actual_popen(
                    [
                        sys.executable,
                        "-I",
                        "-S",
                        "-B",
                        "-c",
                        workspace_service.PROXY,
                        port,
                    ],
                    **options,
                )
                started.append(child_process)
                return child_process

            server, client = socket.socketpair()
            with (
                server,
                client,
                patch.object(
                    web_named_relay.subprocess, "Popen", side_effect=local_relay
                ),
            ):
                catalog.NamedStudioCatalog(owner).respond(
                    server, request(origins, token), monotonic() + 5, check=lambda: None
                )
                server.shutdown(socket.SHUT_WR)
                client.settimeout(1)
                with client.makefile("rb") as stream:
                    head, _, body = stream.read(16384).partition(b"\r\n\r\n")
            require(
                len(started) == 1
                and started[0].returncode == 0
                and head.startswith(b"HTTP/1.1 200 OK\r\n")
                and b"set-cookie" not in head.lower()
                and json.loads(body) == {"sessions": expected, "hasEarlier": True},
                "Catalog returned an unexpected page or did not reap the exact relay.",
            )
        finally:
            owner.close()
        require(app.relay.wait_drained(0), "Catalog grant did not drain.")
    require(
        native("GET", query) == baseline
        and native("GET", "/api/session/active") == {"data": {}}
        and native("GET", "/api/credential") == {"data": []},
        "Catalog changed native sessions, activity or credentials.",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", type=Path, required=True)
    options = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ocb-catalog-") as directory:
        exercise(str(options.opencode.resolve(strict=True)), Path(directory))
    print(
        "PASS OpenCode 2.4.1: one scoped recent page, managed relay reaped, no inference."
    )


if __name__ == "__main__":
    main()
