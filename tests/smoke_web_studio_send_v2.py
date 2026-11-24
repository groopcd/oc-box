"""Verify the exact 2.4.1 prompt contract against a fresh synthetic native owner.

The fixture uses the same fixed transport and acknowledgment check, but sends
resume:false on its one successful native POST. It cannot run inference.
"""

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from urllib.request import ProxyHandler, Request, build_opener
import base64

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from smoke_session_sync_v2 import bounded, private_environment, require, seed

from oc_box import oc_box as box, opencode_compat, opencode_v2_io, workspace_service
from oc_box import web_studio_send as studio

TEXT = "SYNTHETIC_Studio_send\nUnicode: é🙂\tLiteral <tag>"
CONT = "c" * 64


def audit_schema(native):
    endpoint = native.endpoint["url"]
    token = base64.b64encode(
        ("opencode:" + native.endpoint["password"]).encode()
    ).decode()
    request = Request(
        endpoint.rstrip("/") + "/openapi.json",
        headers={"Authorization": "Basic " + token},
    )
    with build_opener(ProxyHandler({})).open(request, timeout=8) as response:
        spec = json.loads(response.read(2 * 1024 * 1024))
    operation = spec["paths"]["/api/session/{sessionID}/prompt"]["post"]
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    response = operation["responses"]["200"]["content"]["application/json"]["schema"]
    types = spec["components"]["schemas"]
    inbox_user = types["Session.Inbox.User"]
    inbox_payload = types["Session.Inbox.UserPayload"]
    require(
        operation["operationId"] == "session.prompt"
        and operation["parameters"]
        == [
            {
                "name": "sessionID",
                "in": "path",
                "required": True,
                "schema": {"$ref": "#/components/schemas/Session.ID"},
            }
        ]
        and body["required"] == ["text"]
        and body["additionalProperties"] is False
        and body["properties"]["text"] == {"type": "string"}
        and body["properties"]["delivery"]["anyOf"][0]
        == {"$ref": "#/components/schemas/Session.Inbox.Delivery"}
        and types["Session.Inbox.Delivery"]["enum"] == ["steer", "queue"]
        and {"type": "boolean"} in body["properties"]["resume"]["anyOf"]
        and response
        == {
            "type": "object",
            "required": ["data"],
            "additionalProperties": False,
            "properties": {"data": {"$ref": "#/components/schemas/Session.Inbox.User"}},
        }
        and set(inbox_user["required"])
        == {"id", "sessionID", "time", "type", "payload", "delivery"}
        and inbox_payload["required"] == ["text"],
        "Unexpected native 2.4.1 prompt contract.",
    )


def exercise(binary, root):
    project = root / "workspace"
    other = root / "unrelated"
    project.mkdir()
    other.mkdir()
    env = private_environment(root / "native")
    require(
        opencode_compat.version(bounded([binary, "--version"], env, project))
        == "2.4.1",
        "This smoke requires the exact native 2.4.1 contract.",
    )
    previous = Path.cwd()
    os.chdir(project)
    real_popen = subprocess.Popen
    try:
        with (
            patch.dict(os.environ, env, clear=True),
            opencode_v2_io.server(binary) as native,
        ):
            require(
                native("GET", "/api/credential") == {"data": []},
                "Unexpected credentials.",
            )
            audit_schema(native)
            denied = [{"action": "*", "resource": "*", "effect": "deny"}]

            def seed_denied(method, path, body):
                body["info"]["permissions"] = denied
                return native(method, path, body)

            session = seed(seed_denied, "ses_studio_send", project)
            foreign = seed(native, "ses_studio_send_foreign", other)
            route = "/api/session/" + session
            paths = [route + "/message", route + "/inbox", "/api/session/active"]
            original = [native("GET", path) for path in paths]
            permissions = native("GET", route)["data"]["permissions"]
            require(
                permissions == denied,
                "Native fixture did not establish its deny rules.",
            )
            browser = object()
            body = json.loads(studio._native_body(TEXT))
            require(
                body == {"text": TEXT, "delivery": "queue", "resume": True},
                "Production native Send operation changed.",
            )
            # Fixture only. No test creates a resume:true request to a server.
            body["resume"] = False
            quiet_body = json.dumps(body, ensure_ascii=False).encode()
            real_port = native.endpoint["url"].rsplit(":", 1)[1]
            count = 0

            def local_popen(command, **kwargs):
                nonlocal count
                count += 1
                require(
                    command
                    == [
                        "podman",
                        "exec",
                        "-i",
                        "--workdir",
                        "/",
                        CONT,
                        "/usr/bin/env",
                        "-i",
                        "PATH=/usr/bin:/bin",
                        "python3",
                        "-I",
                        "-S",
                        "-B",
                        "-c",
                        workspace_service.PROXY,
                        real_port,
                    ]
                    and TEXT not in " ".join(command)
                    and native.endpoint["password"] not in " ".join(command)
                    and TEXT not in kwargs["env"].values()
                    and kwargs["stderr"] == subprocess.DEVNULL
                    and kwargs["stdin"] is kwargs["stdout"],
                    "Studio did not use the fixed private managed relay.",
                )
                # Replace only the outer Podman exec with its exact isolated
                # relay body, using this synthetic native server on loopback.
                return real_popen(
                    [
                        sys.executable,
                        "-I",
                        "-S",
                        "-B",
                        "-c",
                        workspace_service.PROXY,
                        real_port,
                    ],
                    **kwargs,
                )

            def execute(ident, expected):
                selected = studio.SendScope(
                    "a" * 32,
                    "b" * 32,
                    CONT,
                    native.endpoint["url"],
                    str(project),
                    "2.4.1",
                    ident,
                    1,
                    browser,
                )
                control, owner = socket.socketpair()
                with control, owner:
                    attempt = studio.StudioSend(
                        selected,
                        control=control,
                        observe=lambda _: selected,
                        deadline=time.monotonic() + 6,
                    )
                    with (
                        patch.object(
                            studio.web_named_relay.subprocess,
                            "Popen",
                            side_effect=local_popen,
                        ),
                        patch.object(studio, "_native_body", return_value=quiet_body),
                    ):
                        require(
                            attempt.execute(
                                box,
                                native_password=native.endpoint["password"],
                                text=TEXT,
                            )
                            is expected,
                            "Studio Send result differed from the native fixture expectation.",
                        )
                    require(
                        attempt.cleanup_confirmed, "Managed relay did not exit cleanly."
                    )

            execute(foreign, studio.SendResult.REFUSED)
            require(
                [native("GET", path) for path in paths] == original,
                "Out-of-scope fixture changed native state.",
            )
            execute(session, studio.SendResult.QUEUED)
            inbox = native("GET", route + "/inbox")["data"]
            require(
                len(inbox) == 1
                and inbox[0]["payload"] == {"text": TEXT}
                and inbox[0]["sessionID"] == session
                and inbox[0]["delivery"] == "queue",
                "Native inbox did not contain exactly the queued fixture text.",
            )
            execute(session, studio.SendResult.REFUSED)
            require(
                count == 3
                and native("GET", route + "/inbox")["data"] == inbox
                and native("GET", route + "/message") == original[0]
                and native("GET", "/api/session/active") == original[2] == {"data": {}}
                and native("GET", route)["data"]["permissions"] == permissions
                and native("GET", "/api/credential") == {"data": []}
                and native("GET", "/api/session/" + foreign + "/inbox") == {"data": []},
                "Studio queue smoke changed execution, policy, credentials or other history.",
            )
    finally:
        os.chdir(previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", type=Path, required=True)
    options = parser.parse_args()
    binary = str(options.opencode.resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="oc-box-studio-send-") as directory:
        exercise(binary, Path(directory))
    print(
        "PASS OpenCode 2.4.1: exact prompt schema, scoped queue acknowledgment, no inference."
    )


if __name__ == "__main__":
    main()
