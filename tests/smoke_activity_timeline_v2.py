"""Provider-free native OpenCode 2.4.x activity panel smoke, not Podman acceptance."""

import argparse
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from smoke_native_preferences_v2 import Client
from smoke_session_sync_v2 import bounded, exported, private_environment, require
from smoke_ui_state_v2 import LocalTransport, Observer

from oc_box import opencode_compat, opencode_v2_io, ui_state


SESSION = "ses_activity_example"


@contextmanager
def project_cwd(project):
    previous = Path.cwd()
    os.chdir(project)
    try:
        yield
    finally:
        os.chdir(previous)


def millis(second):
    return int(
        datetime.fromisoformat(f"2026-11-26T14:32:{second:02}+00:00").timestamp() * 1000
    )


def seed(request, project):
    def tool(name, created, **timing):
        return {
            "type": "tool",
            "id": f"activity-{name}",
            "name": name,
            "time": {
                "created": millis(created),
                **{key: millis(value) for key, value in timing.items()},
            },
            "state": {
                "status": "completed",
                "input": {},
                "content": [{"type": "text", "text": "Synthetic result"}],
            },
        }

    request(
        "POST",
        "/api/experimental/session/import",
        {
            "info": {
                "id": SESSION,
                "projectID": "global",
                "title": "Synthetic activity session",
                "cost": 0,
                "tokens": {
                    "input": 0,
                    "output": 0,
                    "reasoning": 0,
                    "cache": {"read": 0, "write": 0},
                },
                "time": {"created": millis(1), "updated": millis(50)},
                "location": {"directory": str(project)},
            },
            "messages": [
                {
                    "id": "msg_activity_1",
                    "type": "user",
                    "time": {"created": millis(1)},
                    "text": "Synthetic activity prompt",
                },
                {
                    "id": "msg_activity_2",
                    "type": "assistant",
                    "agent": "build",
                    "model": {
                        "id": "synthetic-model",
                        "providerID": "synthetic-provider",
                    },
                    "time": {"created": millis(2), "completed": millis(49)},
                    "content": [
                        tool("shell", 3, ran=7, completed=40),
                        tool("read", 4, completed=38),
                        {"type": "text", "text": "Synthetic activity reply"},
                    ],
                },
                {
                    "id": "msg_activity_3",
                    "type": "idle",
                    "outcome": "succeeded",
                    "time": {"created": millis(50)},
                },
            ],
            "location": {"directory": str(project)},
        },
    )


def select_command(client, query, title):
    client.watch("Search commands")
    client.send(b"\x10")
    client.wait(lambda: client.labels["Search commands"], "command palette")
    client.watch(title)
    client.send(query.encode())
    client.wait(lambda: client.labels[title], title)


def exercise(binary, root):
    project = root / "project"
    project.mkdir()
    env = private_environment(root / "native")
    env.update(TERM="xterm-256color", COLORTERM="truecolor", TZ="UTC")
    release = opencode_compat.version(bounded([binary, "--version"], env, project))
    require(
        release in {"2.4.0", "2.4.1"},
        "The native fixture targets OpenCode 2.4.0/2.4.1.",
    )
    config = Path(env["XDG_CONFIG_HOME"]) / "opencode"
    config.mkdir(exist_ok=True)
    destination = config / "plugins/oc-box-ui-state"
    destination.mkdir(parents=True)
    package = Path(__file__).resolve().parents[1] / "src/oc_box"
    for name, content in ui_state.package_sources(package, include_addons=True).items():
        (destination / name).write_bytes(content)
    # Use the same combined package discovery as the owning workspace. The
    # native service and its plugin policy are synthetic, with no providers.
    server_config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    server_config.update({"plugins": [str(destination)], "policies": []})
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(server_config)
    cli_config = config / "cli.json"
    cli_config.write_text(
        json.dumps(
            {
                "highlights": False,
                "animations": False,
            }
        )
    )
    with (
        project_cwd(project),
        patch.dict(os.environ, env, clear=True),
        opencode_v2_io.server(binary) as native,
    ):
        require(
            native("GET", "/api/credential") == {"data": []},
            "Unexpected native credentials.",
        )
        seed(native, project)
        connection = {**native.endpoint, "version": release, "workspace": "a" * 32}
        transport = LocalTransport(binary, env, project, connection)
        require(
            ui_state.available(transport.box, transport.data, connection, timeout=10),
            "The native service did not load the bundled UI helper.",
        )
        before = exported(native, SESSION)
        inbox = f"/api/session/{SESSION}/inbox"
        checks = [native("GET", route) for route in (inbox, "/api/session/active")]
        with Observer(native.endpoint) as observer:
            endpoint = {**native.endpoint, "url": observer.url}
            for enabled in (True, False):
                client = Client(
                    binary,
                    {**env, **({"OC_BOX_ACTIVITY_TIMELINE": "1"} if enabled else {})},
                    project,
                    endpoint,
                    cli_config,
                    session_id=SESSION,
                )
                try:
                    client.watch("Synthetic activity reply")
                    client.wait(
                        lambda: client.labels["Synthetic activity reply"],
                        "synthetic session",
                    )
                    ready = "Open activity timeline" if enabled else "No results found"
                    select_command(client, "activity timeline", ready)
                    if enabled:
                        labels = (
                            "Loaded transcript",
                            "2026-11-26 14:32:07 +00:00",
                            "shell invoked",
                            "2026-11-26 14:32:04 +00:00",
                            "read call created",
                            "invocation time unknown",
                            "2026-11-26 14:32:02 +00:00",
                            "2026-11-26 14:32:01 +00:00",
                        )
                        client.watch(*labels)
                        client.send(b"\r")
                        for label in labels:
                            client.wait(
                                lambda: client.labels[label], "activity panel: " + label
                            )
                        client.send(b"\x1b")
                        client.watch("Open activity timeline")
                        client.send(b"/activity-time")
                        client.wait(
                            lambda: client.labels["Open activity timeline"],
                            "slash command after closing the panel",
                        )
                        client.watch("Loaded transcript")
                        client.send(b"\r")
                        client.wait(
                            lambda: client.labels["Loaded transcript"],
                            "activity panel from slash command",
                        )
                    client.send(b"\x1b")
                    select_command(client, "Exit the", "Exit the app")
                    client.send(b"\r", 0.6)
                finally:
                    client.close()
            require(
                observer.bad_posts == 0 and not observer.errors,
                "Unexpected native mutation or observer error.",
            )
        after = exported(native, SESSION)
        require(
            before["messages"] == after["messages"],
            "Opening the panel changed native messages.",
        )
        require(
            before["info"]["tokens"] == after["info"]["tokens"],
            "Opening the panel changed native token usage.",
        )
        require(
            checks
            == [native("GET", route) for route in (inbox, "/api/session/active")],
            "Opening the panel changed native work state.",
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--opencode",
        required=True,
        type=lambda value: str(Path(value).resolve(strict=True)),
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="oc-box-activity-timeline-") as directory:
        exercise(args.opencode, Path(directory))
    print(
        "Native activity panel: on/off, dates and invocation times verified; messages, tokens and work state unchanged."
    )


if __name__ == "__main__":
    main()
