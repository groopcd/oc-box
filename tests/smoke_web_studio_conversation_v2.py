"""Provider-free Studio conversation smoke against unmodified OpenCode 2.4.1."""

import argparse
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from smoke_session_sync_v2 import bounded, private_environment, require, seed

from oc_box import oc_box as box, opencode_compat, opencode_v2_io, workspace_service
from oc_box import web_admission, web_named_owner
from oc_box import web_studio_conversation as studio
from oc_box import web_studio_read as read


def verify_refusal(callback):
    try:
        callback()
    except studio.ConversationError:
        return
    require(False, "Studio accepted an out-of-scope conversation.")


def verify_registered_read(launcher, data, connection, ident, sibling_id, expected):
    owner = web_named_owner.OwnerIdentity(
        data["id"],
        "b" * 32,
        data["container"],
        connection["version"],
        connection["url"],
    )
    binding = web_admission.WebBinding(
        owner.workspace_id, owner.generation, "https://workspace.localhost"
    )
    admission = web_admission.BrowserAdmission(binding)
    token = admission.issue(binding, principal="synthetic")
    browser = admission.lookup(binding, token)
    control, peer = socket.socketpair()
    with control, peer:
        lease = web_named_owner.VerifiedOwnerLease(
            owner,
            control,
            data["container_project"],
            data["home"],
            connection["password"],
            observe=lambda: owner,
            connect_native=lambda *_: require(False, "Unexpected native relay."),
        )
        responses = []

        def write(response, deadline, *, check):
            check()
            responses.append(json.loads(response.body))

        for generation, selected in enumerate((ident, sibling_id)):
            scope = read.ReadScope(owner, browser, selected, generation)
            try:
                read.respond(
                    launcher,
                    read.ReadRequest("GET", read.PATH + "?session=" + selected, b""),
                    scope,
                    lease=lease,
                    admission=admission,
                    binding=binding,
                    observe=lambda _: scope,
                    deadline=monotonic() + 6,
                    write=write,
                    interrupt=lambda: None,
                    retire=lambda: True,  # Synthetic writer owns no stream.
                )
            except read.ReadError:
                require(selected == sibling_id, "Registered Studio read failed.")
            else:
                require(selected == ident, "Registered Studio disclosed a sibling.")
        require(responses == [expected], "Registered Studio projection differed.")
        require(
            admission.begin_revocation(binding, browser).wait_drained(0),
            "Registered Studio worker did not drain.",
        )


def exercise(binary, root):
    repo = root / "repository"
    project, sibling = repo / "workspace", repo / "sibling"
    project.mkdir(parents=True)
    sibling.mkdir()
    foreign = root / "foreign"
    foreign.mkdir()
    env = private_environment(root / "native")
    bounded(["git", "init", "-q"], env, repo)
    bounded(
        [
            "git",
            "-c",
            "user.name=Studio Fixture",
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
    require(release == studio.NATIVE_VERSION, "Studio smoke requires native 2.4.1.")
    data = {
        "id": "a" * 32,
        "container": "c" * 64,  # Synthetic immutable ID; Podman is never invoked.
        "project": str(root / "different-host-project-path"),
        "container_project": str(project),
        "home": env["HOME"],
        "opencode_version": release,
    }

    def local_exec(launcher, data, connection, arguments):
        # Preserve production argv/environment construction; replace only
        # Podman exec with the local child and its sanitized outer environment.
        command = workspace_service.client_command(
            launcher, data, connection, arguments
        )
        offset = command.index(data["container"])
        require(
            command[:offset]
            == [
                "podman",
                "exec",
                "-i",
                "--workdir",
                str(project),
                "--env",
                "OPENCODE_PASSWORD",
            ]
            and command[offset + 1 : offset + 5] == ["python3", "-I", "-S", "-c"]
            and command[-3:] == [connection["url"], str(project), arguments[-1]]
            and connection["password"] not in " ".join(command)
            and not any("SYNTHETIC_PROVIDER" in name for name in command.environment),
            "Studio lost the providerless private exec boundary.",
        )
        if arguments[-4] == studio._MANAGED_ARG:
            # The managed host exec starts in '/'. Model Podman's --workdir
            # before running the exact same sanitized Python command locally.
            return box.ContainerCommand(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-B",
                    "-c",
                    "import os,sys; os.chdir(sys.argv[1]); os.execv(sys.argv[2], sys.argv[2:])",
                    str(project),
                    sys.executable,
                    *command[offset + 2 :],
                ],
                command.environment,
            )
        return box.ContainerCommand(
            [sys.executable, *command[offset + 2 :]], command.environment
        )

    launcher = SimpleNamespace(
        BoxError=box.BoxError,
        maintenance_host_env=box.maintenance_host_env,
        ContainerCommand=box.ContainerCommand,
        container_project=box.container_project,
        workspace_service=SimpleNamespace(
            validate_endpoint=workspace_service.validate_endpoint,
            client_command=local_exec,
        ),
    )
    previous = Path.cwd()
    os.chdir(project)
    exit_info = {}
    try:
        with (
            patch.dict(os.environ, env, clear=True),
            opencode_v2_io.server(binary, exit_info=exit_info) as native,
        ):
            require(
                native("GET", "/api/credential") == {"data": []},
                "Unexpected credentials.",
            )
            ident = "ses_studio_example"
            route = "/api/session/" + ident
            messages = [
                {
                    "id": f"msg_studio{i:03}",
                    "type": "user",
                    "time": {"created": i + 1},
                    "metadata": {"secret": "OMITTED_METADATA"},
                    "text": f"Synthetic prompt {i}",
                }
                for i in range(studio.PAGE_SIZE + 1)
            ]
            messages.extend(
                (
                    {
                        "id": "msg_studio_assistant",
                        "type": "assistant",
                        "agent": "build",
                        "model": {
                            "id": "synthetic-model",
                            "providerID": "synthetic-provider",
                        },
                        "time": {"created": 900, "completed": 901},
                        "content": [
                            {"type": "reasoning", "text": "OMITTED_REASONING"},
                            {
                                "type": "tool",
                                "id": "synthetic-call",
                                "name": "synthetic-tool",
                                "time": {"created": 900},
                                "state": {
                                    "status": "completed",
                                    "input": {"value": "OMITTED_TOOL_INPUT"},
                                    "content": [
                                        {"type": "text", "text": "OMITTED_TOOL_OUTPUT"}
                                    ],
                                },
                            },
                            {"type": "text", "text": "<script>literal text</script>"},
                            {"type": "text", "text": "Final answer"},
                        ],
                    },
                    {
                        "id": "msg_studio_idle",
                        "type": "idle",
                        "outcome": "succeeded",
                        "time": {"created": 902},
                    },
                )
            )
            native(
                "POST",
                "/api/experimental/session/import",
                {
                    "info": {
                        "id": ident,
                        "projectID": "global",
                        "title": "Synthetic Studio",
                        "cost": 0,
                        "tokens": {
                            "input": 0,
                            "output": 0,
                            "reasoning": 0,
                            "cache": {"read": 0, "write": 0},
                        },
                        "time": {"created": 1, "updated": 2},
                        "location": {"directory": str(project)},
                    },
                    "messages": messages,
                    "location": {"directory": str(project)},
                },
            )
            sibling_id = seed(native, "ses_studio_sibling", sibling)
            foreign_id = seed(native, "ses_studio_foreign", foreign)
            other_id = seed(native, "ses_studio_other", project)
            moved_id = seed(native, "ses_studio_moved", project)
            moved_route = "/api/session/" + moved_id
            for directory in (foreign, project):
                native("POST", moved_route + "/move", {"directory": str(directory)})
                native("POST", "/api/experimental/session/" + moved_id + "/wait")
                require(
                    native("GET", moved_route)["data"]["location"]["directory"]
                    == str(directory),
                    "Synthetic native move failed.",
                )
            native_location = native("GET", "/api/location")
            native_sibling = native("GET", "/api/session/" + sibling_id)["data"]
            require(
                native_location["project"]["id"] == native_sibling["projectID"]
                and native_location["directory"]
                != native_sibling["location"]["directory"],
                "Studio smoke did not establish the same-project, wrong-directory case.",
            )
            wrong_message_route = "/api/session/" + other_id + "/message/msg_studio000"
            try:
                native("GET", wrong_message_route)
            except HTTPError as error:
                require(
                    error.code == 404,
                    "Native GET did not bind the message to its session.",
                )
                error.close()
            else:
                require(False, "Native GET accepted a different session's message.")

            paths = [route, route + "/message?limit=100&order=asc", route + "/inbox"]
            before = [native("GET", path) for path in paths]
            connection = {
                **native.endpoint,
                "workspace": data["id"],
                "version": release,
            }
            with patch.dict(os.environ, {"SYNTHETIC_PROVIDER_API_KEY": "sentinel"}):
                result = studio.conversation(launcher, data, connection, ident)
                verify_refusal(
                    lambda: studio.conversation(launcher, data, connection, sibling_id)
                )
                verify_refusal(
                    lambda: studio.conversation(launcher, data, connection, foreign_id)
                )
                verify_refusal(
                    lambda: studio.conversation(launcher, data, connection, moved_id)
                )
                verify_refusal(
                    lambda: studio.conversation(
                        launcher, data, connection, "ses_studio_absent"
                    )
                )
            expected = [
                {
                    "id": row["id"],
                    "role": "user",
                    "text": row["text"],
                    "truncated": False,
                }
                for row in messages
                if row["type"] == "user"
            ]
            expected.append(
                {
                    "id": "msg_studio_assistant",
                    "role": "assistant",
                    "text": "<script>literal text</script>\n\nFinal answer",
                    "truncated": False,
                }
            )
            require(
                result
                == {
                    "sessionID": ident,
                    "title": "Synthetic Studio",
                    "messages": expected,
                    "hasEarlier": False,
                },
                "Native Studio projection differed from the expected ordered plain text.",
            )
            with patch.dict(os.environ, {"SYNTHETIC_PROVIDER_API_KEY": "sentinel"}):
                verify_registered_read(
                    launcher, data, connection, ident, sibling_id, result
                )
            require(
                [native("GET", path) for path in paths] == before
                and native("GET", "/api/session/active") == {"data": {}},
                "Studio reads changed native history, inbox, session or activity.",
            )
    finally:
        os.chdir(previous)
    require(
        exit_info == {"code": 0, "forced": False}, "Native smoke did not stop cleanly."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", type=Path, required=True)
    options = parser.parse_args()
    binary = str(options.opencode.resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="oc-box-studio-conversation-") as directory:
        exercise(binary, Path(directory))
    print("PASS OpenCode 2.4.1: scoped, paginated, read-only Studio transcript.")


if __name__ == "__main__":
    main()
