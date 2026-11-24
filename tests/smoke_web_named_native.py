"""Provider-free named owner/pair/relay smoke against exact native binaries.

Only the Podman boundary is substituted: inspect returns synthetic ownership
facts and exec starts the actual worker locally. No TLS listener, shared
service discovery, real workspace, browser credentials, or provider traffic.
"""

import argparse
import base64
import contextlib
from hashlib import sha256
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
from time import monotonic
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from smoke_session_sync_v2 import bounded, private_environment, require
import test_web_pake_flow as pake

from oc_box import (
    oc_box as box,
    web_app_ingress,
    web_local_origins,
    web_named_owner,
    web_named_protocol,
    web_named_transport,
    web_native_pair as pair,
    workspace_control,
    workspace_service,
)


_CONTAINER = "e" * 64


def build_environment(root, version):
    env = private_environment(root)
    plugin = Path(env["HOME"]) / ".config/opencode/plugins/oc-box-build-permissions"
    plugin.mkdir(parents=True, mode=0o700)
    (plugin / "package.json").write_text('{"type":"module","main":"index.js"}')
    (plugin / "index.js").write_text(box.container_build_plugin(version))
    overlay = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    overlay.update(box.container_build_config(version, home=env["HOME"]))
    overlay["policies"] = [
        {"action": "provider.use", "resource": "*", "effect": "deny"}
    ]
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(overlay)
    return env


def fixture_inspect(data):
    info = {
        "Id": _CONTAINER,
        "State": {
            "Status": "running",
            "Running": True,
            "Paused": False,
            "Restarting": False,
            "StartedAt": "2026-02-01T04:05:06.123456789Z",
        },
        "Config": {"Labels": {box.LABEL: data["id"]}},
        "HostConfig": {"NetworkMode": "pasta", "PortBindings": {}},
        "Mounts": [
            {"Type": "bind", "Source": data["project"], "Destination": data["project"]},
            {
                "Type": "volume",
                "Name": data["home_volume"],
                "Destination": data["home"],
            },
        ],
    }

    def inspect(*args, **options):
        require(
            args[:2] == ("container", "inspect")
            and args[-1] in (data["container"], _CONTAINER)
            and options["private"]
            and options["stderr"] == subprocess.DEVNULL,
            "Unsafe native fixture inspection.",
        )
        options["stdout"].write(json.dumps([info]).encode())
        return subprocess.CompletedProcess(args, 0)

    return inspect


@contextlib.contextmanager
def local_pair_exec(project, lease):
    spawn = subprocess.Popen

    def run(command, **options):
        marker = command.index(_CONTAINER)
        require(
            command[:marker]
            == [
                "podman",
                "exec",
                "-i",
                "--workdir",
                str(project),
                "--env",
                "OPENCODE_PASSWORD",
            ]
            and command[marker + 1 : marker + 5] == ["python3", "-I", "-S", "-c"]
            and command[-3] == lease.identity.endpoint
            and all(lease.native_password not in arg for arg in command)
            and "OPENCODE_CONFIG_CONTENT" not in options["env"]
            and "SYNTHETIC_PROVIDER_API_KEY" not in options["env"],
            "Private providerless pairing command was not preserved.",
        )
        return spawn([sys.executable, *command[marker + 2 :]], cwd=project, **options)

    with patch.object(pair.subprocess, "Popen", side_effect=run):
        yield


def native_wire(lease, origins, cookie):
    identity = lease.identity
    version, endpoint = identity.version, identity.endpoint
    port = pair._port(endpoint)
    authority = origins.workspace.value.removeprefix("https://")
    auth = "Basic " + base64.b64encode(
        ("opencode:" + lease.native_password).encode("ascii")
    ).decode("ascii")

    def exchange(route, **kw):
        return pair._exchange(port, authority, monotonic() + 3, route, **kw)

    # Private synthetic service only. Do not emit any response bytes or secrets.
    before = pair._identity(exchange("/api/info", auth=auth), endpoint, version)
    require(
        exchange("/api/info")[0] == 401
        and exchange("/api/info", cookie=cookie.split("=", 1)[0] + "=invalid")[0] == 401
        and pair._identity(exchange("/api/info", cookie=cookie), endpoint, version)
        == before,
        "The native cookie identity or authentication fence changed.",
    )
    require(
        pair._json_reply(exchange("/api/credential", auth=auth)) == {"data": []},
        "Unexpected native credentials in synthetic service.",
    )
    minted = pair._json_reply(exchange("/api/pair", method="POST", auth=auth))
    require(
        minted.keys() == {"code", "expires_in"}
        and pair._CODE.fullmatch(minted["code"]) is not None
        and minted["expires_in"] == 300,
        "Native pairing code format changed.",
    )
    route = "/auth/connect/" + minted["code"]
    response = exchange(route)
    value = pair._parse_cookie(response, authority)
    replay = exchange(route)
    require(
        pair._identity(
            exchange("/api/info", cookie=pair._cookie_name(authority) + "=" + value),
            endpoint,
            version,
        )
        == before
        and replay[0] == 401
        and not {"set-cookie", "set-cookie2"} & replay[1].keys(),
        "Native one-use pairing or cookie scope changed.",
    )
    return {"version": version, "pid": before["pid"], "urls": before["urls"]}


def relay_wire(bound, origins, cookie, identity):
    # Trusted synthetic admission isolates the native relay from PAKE/TLS
    # fixtures. The attached socket, native request and response are real.
    binding = bound.binding
    account = pake.accounts.Account("fixture", (binding.workspace_id,), pake.REAL)
    flow = pake.pake.PakeFlow(
        binding,
        store=pake.accounts.AccountStore(pake.SETUP, (account,)),
        backend=pake.SyntheticBackend(),
        sizes=pake.SIZES,
        auth_origin=origins.account.value,
    )
    try:
        app = bound.compose(flow=flow, claim_bootstrap=lambda _: None)
        token = app.sign_in.admission.issue(
            binding, principal="fixture", native_cookie=cookie
        )
        require(token is not None, "Synthetic browser grant could not be issued.")
        for route, expected in (
            ("/api/info", identity),
            ("/api/session/active", {"data": {}}),
        ):
            request = web_named_transport.NamedRequest(
                "workspace",
                "GET",
                route,
                (
                    ("Host", origins.workspace.value.removeprefix("https://")),
                    ("Origin", origins.workspace.value),
                    ("Cookie", web_app_ingress.APP_COOKIE_NAME + "=" + token),
                ),
                b"",
            )
            server, client = socket.socketpair()
            with server, client:
                app.relay._respond(server, request, monotonic() + 5)
                client.settimeout(1)
                with client.makefile("rb") as stream:
                    wire = stream.read(8192)
            headers, _, body = wire.partition(b"\r\n\r\n")
            require(
                headers.startswith(b"HTTP/1.1 200 OK\r\n")
                and b"set-cookie" not in headers.lower()
                and json.loads(body) == expected,
                "The named relay did not return only audited public metadata.",
            )
    finally:
        bound.close()
        flow.close()
    require(app.relay.wait_drained(2), "Native relay cleanup was not confirmed.")


def exercise(binary, version, root):
    env = build_environment(root / "native", version)
    env["OC_BOX_STATE_HOME"] = str(root / "registry")
    env["XDG_RUNTIME_DIR"] = str(root)
    project = root / "project"
    project.mkdir(mode=0o700)
    require(
        bounded([binary, "--version"], env, project).strip() == "opencode v" + version,
        "Unexpected native executable release.",
    )

    def exec_worker(data, arguments):
        require(
            arguments[:2] == ["python3", "-c"]
            and arguments[3:5] == ["named-lease", box.CONTAINER_OPENCODE]
            and arguments[5:] == [data["opencode_version"]]
            and data["container"] == _CONTAINER,
            "Named readiness lost its exact immutable target.",
        )
        return box.ContainerCommand(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-c",
                arguments[2],
                "named-lease",
                binary,
                arguments[5],
            ],
            env,
        )

    def connect(identity, *, track, check, deadline):
        # Only the existing disposable native service in this fixture.
        check()
        peer = track(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        peer.settimeout(deadline - monotonic())
        peer.connect(("127.0.0.1", pair._port(identity.endpoint)))
        check()

    other = {"2.3.2": "2.4.1", "2.4.1": "2.3.2"}[version]
    cwd = Path.cwd()
    os.chdir(project)
    try:
        with patch.dict(os.environ, env, clear=True):
            path, data = box.create_workspace(project, "tumbleweed")
            data.update(ready=True, opencode_version=version)
            box.atomic_json(path, data)
            with (
                patch.object(box, "podman", side_effect=fixture_inspect(data)),
                patch.object(box, "exec_command", side_effect=exec_worker),
                box.workspace_lock(path),
            ):
                stop = workspace_control.capture(path)
                owner = workspace_service.Owner(box, path, data, named_stop_token=stop)
                with owner:
                    owner.publish(None)
                    with web_named_protocol.verified_owner_lease(
                        owner, connect_native=connect
                    ) as lease:
                        require(
                            lease.identity.version == version, "Wrong lease version."
                        )
                        origins = web_local_origins.LocalOrigins()
                        bound = web_named_owner.NamedOwnerBinding(
                            box, origins=origins, lease=lease
                        )
                        try:
                            with local_pair_exec(project, lease):
                                cookie = bound._pair_native(
                                    bound.binding, origins.workspace
                                )
                                # A matching metadata label alone cannot make
                                # the worker pair with another audited release.
                                try:
                                    pair.pair_cookie(
                                        box,
                                        {
                                            **data,
                                            "opencode_version": other,
                                            "container": _CONTAINER,
                                        },
                                        {**owner.connection, "version": other},
                                        origin=origins.workspace,
                                        binding=bound.binding,
                                        owner_current=bound._owner_current,
                                    )
                                except pair.NativePairError:
                                    pass
                                else:
                                    require(
                                        False,
                                        "Native pairing accepted a different declared release.",
                                    )
                            private = cookie.upstream_cookie(
                                binding=bound.binding,
                                endpoint=lease.identity.endpoint,
                                origin=origins.workspace,
                                version=version,
                            )
                            public = native_wire(lease, origins, private)
                            relay_wire(bound, origins, cookie, public)
                        finally:
                            bound.close()
                require(
                    owner.child.returncode == 0,
                    "Named native worker did not stop cleanly.",
                )

                wrong = {**data, "opencode_version": other}
                refused = workspace_service.Owner(
                    box, path, wrong, named_stop_token=stop
                )
                try:
                    with refused:
                        require(
                            False,
                            "Named readiness admitted a different audited release.",
                        )
                except box.BoxError:
                    require(
                        refused.child is not None
                        and refused.child.returncode == 2
                        and not refused.address.exists(),
                        "Mismatched native readiness did not refuse and cleanly stop.",
                    )
    finally:
        os.chdir(cwd)


def digest(path):
    result = sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode-232", required=True, type=Path)
    parser.add_argument("--opencode-241", required=True, type=Path)
    options = parser.parse_args()

    def expired(*_):
        raise TimeoutError()

    signal.signal(signal.SIGALRM, expired)
    for version, path in (
        ("2.3.2", options.opencode_232),
        ("2.4.1", options.opencode_241),
    ):
        try:
            path = path.resolve(strict=True)
            with tempfile.TemporaryDirectory(prefix="ocb-named-wire-") as directory:
                signal.alarm(45)
                try:
                    exercise(str(path), version, Path(directory))
                finally:
                    signal.alarm(0)
            print(
                f"PASS {version} sha256={digest(path)}: ready, paired, projected, and stopped."
            )
        except Exception as error:
            # Never include raw native data, errors, service addresses, or keys.
            print(f"FAIL {version}: {type(error).__name__}.", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
