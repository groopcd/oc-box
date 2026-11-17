"""Provider-free native TUI membership -> authenticated web index regression.

Run once per binary with --opencode PATH (2.2.6 or 2.3.0). Uses fresh synthetic
HOME/XDG stores, native PTYs, native cookie pairing and actual paginated HTTP.
Only the outer Podman exec boundary is replaced with local processes. No
browser rendering, shared host service, Podman or host-acceptance claim.
"""

import argparse
from http.cookiejar import CookieJar
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from smoke_native_tabs_v2 import Client
from smoke_session_sync_v2 import bounded, private_environment, require, seed

from oc_box import native_tabs, oc_box as box, opencode_compat
from oc_box import opencode_v2_io as bridge, web_sessions, workspace_service as service


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sessions = []
        self.home = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "a" and values.get("href", "").startswith("/server/"):
            self.sessions.append(values["href"])
        if tag == "a" and values.get("href") == "/":
            self.home.append(values["href"])


def fresh_home(native, data, release, home, project, local_exec, local_relay):
    """Unbound HTTP-only navigation; no session create/open/prompt mutations."""
    snapshot = native_tabs.read_snapshot(str(home), str(project), release)
    require(snapshot.status == "missing", "Fresh Home fixture had TUI membership.")
    before = bridge.service_session_catalog(native)
    require(before == [], "Fresh Home fixture had existing sessions.")
    connection = {**native.endpoint, "workspace": data["id"], "session": None}
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
    with (
        patch.object(box, "run", side_effect=local_exec),
        patch.object(service, "relay_command", side_effect=local_relay),
        service.Gateway(box, data, connection) as gateway,
    ):
        pair = native("POST", "/api/pair")
        request = Request(
            gateway.url + "/auth/connect/" + pair["code"],
            headers={"Accept": "text/html"},
        )
        with opener.open(request, timeout=15) as response:
            require(
                response.geturl() == gateway.url + web_sessions.INDEX,
                "Fresh pairing did not land on the session index.",
            )
            links = Links()
            links.feed(response.read().decode())
        require(
            links.sessions == [] and links.home == ["/"],
            "Empty index did not offer native Home without fabricated rows.",
        )
        with opener.open(gateway.url + links.home[0], timeout=10) as response:
            require(
                response.status == 200
                and "text/html" in response.headers["Content-Type"]
                and response.read(),
                "Native Home did not serve its public web shell.",
            )
    require(
        native_tabs.read_snapshot(str(home), str(project), release) == snapshot,
        "Fresh Home navigation changed TUI membership.",
    )
    require(
        bridge.service_session_catalog(native) == before,
        "Fresh Home navigation created or changed native sessions.",
    )
    print(
        "PASS native " + release + ": fresh unbound pairing, empty chooser "
        "with explicit native Home, public Home shell, unchanged missing TUI "
        "state and empty private session inventory; no session mutations.",
        flush=True,
    )


def exercise(binary, root, *, home_only=False):
    project = root / "project"
    project.mkdir()
    foreign = root / "other-project"
    foreign.mkdir()
    worktree = root / "worktree"
    moved = root / "moved-project"
    moved.mkdir()
    env = private_environment(root / "native")
    if not home_only:
        bounded(["git", "init", "-q"], env, project)
        (project / "fixture.txt").write_text(
            "Synthetic web session worktree fixture.\n"
        )
        bounded(["git", "add", "fixture.txt"], env, project)
        bounded(
            [
                "git",
                "-c",
                "user.name=Synthetic Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "Synthetic",
            ],
            env,
            project,
        )
        bounded(
            ["git", "worktree", "add", "--quiet", "-b", "synthetic-web", str(worktree)],
            env,
            project,
        )
    home = Path(env["HOME"])
    for name, relative in (
        ("CONFIG", ".config"),
        ("STATE", ".local/state"),
        ("DATA", ".local/share"),
        ("CACHE", ".cache"),
    ):
        env["XDG_" + name + "_HOME"] = str(home / relative)
    env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor"})
    config = home / ".config/opencode"
    config.mkdir(parents=True)
    (config / "cli.json").write_text(
        json.dumps(
            {
                "tabs": {"mode": "on", "scope": "cwd", "layout": "horizontal"},
                "animations": False,
                "highlights": False,
            }
        )
    )
    release = opencode_compat.version(bounded([binary, "--version"], env, project))
    require(release in ("2.2.6", "2.3.0"), "Web fixture requires 2.2.6 or 2.3.0.")
    data = {
        "id": "a" * 32,
        "project": str(root / "host-restored-location"),
        "container_project": str(project),
        "home": str(home),
        "container": "synthetic-no-podman",
        "opencode_version": release,
    }
    original_run = box.run

    def local_exec(command, **settings):
        marker = command.index(data["container"])
        require(
            command[:3] == ["podman", "exec", "-i"]
            and command[marker + 1 : marker + 5] == ["python3", "-I", "-S", "-c"],
            "Web fixture lost the providerless production environment bridge.",
        )
        if "input" not in settings:
            settings["stdin"] = subprocess.DEVNULL
        return original_run(
            command[marker + 1 :],
            cwd=project,
            env=command.environment,
            **settings,
        )

    def local_relay(_box, _data, port):
        return box.ContainerCommand(
            [sys.executable, "-B", "-c", service.PROXY, str(port)],
            box.maintenance_host_env(),
        )

    previous = Path.cwd()
    os.chdir(project)
    try:
        with patch.dict(os.environ, env, clear=True), bridge.server(binary) as native:
            require(
                native("GET", "/api/credential")["data"] == [],
                "Web fixture inherited credentials.",
            )
            fresh_home(native, data, release, home, project, local_exec, local_relay)
            if home_only:
                return
            opened = [
                seed(
                    native,
                    "ses_web_" + str(index),
                    worktree if index == 1 else project,
                    title="Synthetic " + str(index),
                )
                for index in range(4)
            ]
            closed = seed(native, "ses_web_closed", project)
            seed(native, "ses_web_child", project, parent=opened[0])
            seed(native, "ses_web_foreign", foreign)
            for index, ident in enumerate(opened):
                client = Client(binary, env, project, native.endpoint, ident)
                try:
                    client.wait(
                        lambda: (
                            native_tabs.read_snapshot(
                                str(home), str(project), release
                            ).tabs
                            == tuple(opened[: index + 1])
                        ),
                        "web fixture native tab membership",
                    )
                    # Membership can persist before native startup/hydration
                    # finishes; retain the established PTY fixture's settle.
                    client.drain(1.3)
                    if index == 3:
                        native(
                            "POST",
                            "/api/session/" + opened[2] + "/move",
                            {"directory": str(moved)},
                        )
                        native("PATCH", "/api/session/" + opened[3], {"archived": True})
                        client.drain(1.3)
                        require(
                            native_tabs.read_snapshot(
                                str(home), str(project), release
                            ).tabs
                            == tuple(opened),
                            "Native moving/archiving removed an explicitly open tab.",
                        )
                finally:
                    client.close()
                print("Native TUI tabs opened: " + str(index + 1), flush=True)
            require(
                native("GET", "/api/session/active")["data"] == {},
                "Idle web fixture unexpectedly had an active foreground drain.",
            )
            before_tabs = native_tabs.read_snapshot(str(home), str(project), release)
            before_ids = {info["id"] for info in bridge.service_session_catalog(native)}
            before = {
                ident: native("GET", "/api/experimental/session/" + ident + "/export")[
                    "data"
                ]
                for ident in [*opened, closed]
            }
            require(
                before[opened[1]]["info"]["location"]["directory"] == str(worktree)
                and before[opened[2]]["info"]["location"]["directory"] == str(moved)
                and before[opened[3]]["info"]["time"].get("archived") is not None,
                "Native cross-directory/archive fixture did not establish its cases.",
            )
            connection = {
                **native.endpoint,
                "workspace": data["id"],
                "session": opened[0],
            }
            opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
            with (
                patch.object(box, "run", side_effect=local_exec),
                patch.object(service, "relay_command", side_effect=local_relay),
                service.Gateway(box, data, connection) as gateway,
            ):
                try:
                    opener.open(gateway.url + web_sessions.INDEX, timeout=10)
                except HTTPError as error:
                    require(
                        error.code == 401, "Native index anonymous refusal changed."
                    )
                else:
                    require(False, "Native index exposed session metadata anonymously.")
                pair = native("POST", "/api/pair")
                request = Request(
                    gateway.url + "/auth/connect/" + pair["code"],
                    headers={"Accept": "text/html"},
                )
                with opener.open(request, timeout=15) as response:
                    require(
                        response.geturl() == gateway.url + web_sessions.INDEX,
                        "Native pairing did not land on the session index.",
                    )
                    parser = Links()
                    parser.feed(response.read().decode())
                require(
                    parser.sessions
                    == [gateway.native_session_route(ident) for ident in opened],
                    "Native paired index did not list exactly four ordered open tabs.",
                )
                for route, ident in zip(parser.sessions, opened):
                    with opener.open(gateway.url + route, timeout=10) as response:
                        require(
                            response.status == 200
                            and "text/html" in response.headers["Content-Type"],
                            "Public native session route did not serve its web shell.",
                        )
                        response.read()
                    with opener.open(
                        gateway.url + "/api/session/" + ident, timeout=10
                    ) as response:
                        require(
                            json.loads(response.read())["data"]["id"] == ident,
                            "Paired native browser could not retrieve a listed session.",
                        )
                try:
                    opener.open(
                        Request(
                            gateway.url + web_sessions.INDEX,
                            headers={"Origin": "http://foreign.invalid"},
                        ),
                        timeout=10,
                    )
                except HTTPError as error:
                    require(
                        error.code == 401, "Native same-origin cookie fence changed."
                    )
                else:
                    require(False, "Index weakened native same-origin cookie checks.")
            require(
                native_tabs.read_snapshot(str(home), str(project), release)
                == before_tabs,
                "Web navigation changed native TUI membership.",
            )
            require(
                {info["id"] for info in bridge.service_session_catalog(native)}
                == before_ids,
                "Web index created or deleted native history.",
            )
            require(
                all(
                    native("GET", "/api/experimental/session/" + ident + "/export")[
                        "data"
                    ]
                    == value
                    for ident, value in before.items()
                ),
                "Web index changed retained synthetic transcripts.",
            )
    finally:
        os.chdir(previous)
    print(
        "PASS native " + release + ": four idle native TUI tabs spanning current "
        "directory, real Git worktree, native move and native archive; authenticated "
        "pairing/index, four public native web routes and session GETs, foreign "
        "origin refusal, unchanged membership and transcripts.",
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True, type=Path)
    parser.add_argument(
        "--home-only",
        action="store_true",
        help="fresh/unbound HTTP-only navigation check; do not start a TUI",
    )
    args = parser.parse_args()
    # Retain this fresh synthetic fixture on failure; never overwrite old evidence.
    root = Path(tempfile.mkdtemp(prefix="oc-box-web-sessions-v2-"))
    print("Synthetic fixture: " + str(root), flush=True)
    exercise(str(args.opencode.resolve()), root, home_only=args.home_only)
    print(
        ("Home-only HTTP mode did not start a TUI. " if args.home_only else "")
        + "Native synthetic regression only; NOT rendered-browser chat, installed "
        "service-worker or host/Podman acceptance.",
        flush=True,
    )


if __name__ == "__main__":
    main()
