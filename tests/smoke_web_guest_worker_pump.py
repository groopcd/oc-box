"""Opt-in local pipe smoke of one-use pairing through the unwired byte pump.

Run with python3 -B tests/smoke_web_guest_worker_pump.py --opencode /path/to/pinned-opencode.
Uses the existing unmodified OpenCode 2.4.1 fixture and synthetic state. No
oc-box Gateway, browser routes, Podman, VM, real credentials, or inference.
"""

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
from unittest.mock import patch

from smoke_session_sync_v2 import FixtureFailure, private_environment, require
from smoke_web_studio_guest_native import _pin

from oc_box import opencode_v2_io, web_guest_duplex_host, web_guest_duplex_worker
from oc_box import web_guest_worker_drain, web_native_pair as pair, web_sessions


_LIMIT = 16500
_WORKER = Path(web_guest_duplex_worker.__file__).resolve()


def _exchange(fixture, workers, authority, target, *, cookie=None, pairing=False):
    fields = [
        f"GET {target} HTTP/1.1",
        "Host: " + authority,
        "Origin: http://" + authority,
        "Accept: text/html" if pairing else "Accept: application/json",
        "Accept-Encoding: identity",
        "Connection: close",
    ]
    if cookie is not None:
        fields.append("Cookie: " + cookie)
    request = ("\r\n".join(fields) + "\r\n\r\n").encode("ascii")
    port = pair._port(fixture.endpoint["url"])
    outcome = []
    finished = Event()
    cancelled = Event()
    expires = monotonic() + 6

    def run(use):
        activity = child = tunnel = None
        native_end = retired = reaped = False

        try:
            activity = workers.admit(use, wake=cancelled.set)
            if activity is None or cancelled.is_set() or not workers.current(use):
                return
            child = subprocess.Popen(
                [sys.executable, "-I", "-S", "-B", str(_WORKER), str(port)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={},
                bufsize=0,
            )
            tunnel = web_guest_duplex_host.DuplexHost(child)
            retired = web_guest_duplex_host.pump_worker(
                workers,
                use,
                tunnel,
                request,
                pairing_path=web_sessions.INDEX if pairing else None,
                deadline=expires,
            )
            native_end = tunnel.ended
            if retired and child.returncode == 0:
                try:
                    os.waitpid(child.pid, os.WNOHANG)
                except ChildProcessError:
                    reaped = True
        finally:
            try:
                if activity is not None:
                    # Only charge/retire this exact transport in this fixture.
                    workers.complete(use, activity, confirmed=retired and reaped)
                if tunnel is not None and not retired:
                    tunnel.abandon()
                if child is not None:
                    if child.poll() is None:  # Emergency fixture-only cleanup.
                        child.kill()
                        child.wait(timeout=2)
                    child.stdin.close()
                    child.stdout.close()
            finally:
                outcome.append((native_end, retired, reaped))
                finished.set()

    stream, browser = socket.socketpair()
    with browser:
        if not workers.dispatch(stream, run):
            stream.close()
            require(False, "Private browser worker was not registered.")
        raw = bytearray()
        # Wait for transport retirement without manufacturing browser EOF.
        # A single bound includes the pump lifetime and its separate cleanup.
        deadline = expires + 6
        while True:
            left = deadline - monotonic()
            require(left > 0, "Private browser worker did not close in time.")
            browser.settimeout(left)
            try:
                data = browser.recv(min(4096, _LIMIT + 1 - len(raw)))
            except TimeoutError:
                raise FixtureFailure(
                    "Private browser worker did not close in time."
                ) from None
            if not data:
                break
            raw.extend(data)
            require(len(raw) <= _LIMIT, "Native response exceeded the fixture bound.")
        require(
            finished.wait(max(0, deadline - monotonic())),
            "Private browser worker did not record its result.",
        )
        return raw, outcome[0]


def _exercise(fixture, workers, evidence):
    endpoint = fixture.endpoint["url"]
    port = pair._port(endpoint)
    authority = "127.0.0.1:" + ("49153" if port == 49152 else "49152")
    expected = fixture("GET", "/api/info")
    pair.named_native_info(expected, "2.4.1", endpoint)
    require(expected["pid"] == fixture.child_pid, "Native server identity mismatch.")
    minted = fixture("POST", "/api/pair")
    require(
        type(minted) is dict
        and set(minted) == {"code", "expires_in"}
        and type(minted["code"]) is str
        and pair._CODE.fullmatch(minted["code"])
        and minted["expires_in"] == 300,
        "Native pairing setup returned an unexpected shape.",
    )
    target = "/auth/connect/" + minted["code"]

    def fetch(stage, route, **options):
        evidence["stage"] = stage
        raw, transport = _exchange(fixture, workers, authority, route, **options)
        evidence["transport"].append((stage, *transport))
        reply = pair._reply(raw)
        require(
            reply[1].get("connection") == ["close"],
            "Response lacked Connection: close.",
        )
        require("keep-alive" not in reply[1], "Response retained Keep-Alive.")
        return reply

    status, headers, body = fetch("pairing", target, pairing=True)
    require(
        headers.get("location") == [web_sessions.INDEX],
        "Pairing redirect was not rewritten.",
    )
    # The existing parser checks the native Set-Cookie attributes and exact
    # body framing. Undo only the already-asserted Location rewrite for it.
    value = pair._parse_cookie((status, dict(headers, location=["/"]), body), authority)
    cookie = pair._cookie_name(authority) + "=" + value
    evidence["paired"] = True
    info = fetch("cookie /api/info", "/api/info", cookie=cookie)
    require(
        pair._identity(info, endpoint, "2.4.1") == expected,
        "Cookie did not identify the same native server.",
    )
    evidence["info"] = True
    replay, headers, _ = fetch("one-use replay", target, pairing=True)
    require(
        replay == 401
        and not {"set-cookie", "set-cookie2", "location"} & headers.keys(),
        "Native replay did not refuse the spent pairing code.",
    )
    evidence["replay"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", type=Path, required=True)
    options = parser.parse_args()
    evidence = {"stage": "native setup", "transport": []}
    try:
        binary = str(options.opencode.resolve(strict=True))
        with TemporaryDirectory(prefix="studio-guest-worker-pump-") as directory:
            root = Path(directory)
            env = private_environment(root / "native")
            project = root / "synthetic-project"
            project.mkdir()
            config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
            config["policies"].append(
                {"action": "provider.use", "resource": "*", "effect": "deny"}
            )
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
            _pin(binary, env, project)
            cwd, exit_info = Path.cwd(), {}
            os.chdir(project)
            try:
                with (
                    patch.dict(os.environ, env, clear=True),
                    opencode_v2_io.server(
                        binary,
                        exit_info=exit_info,
                        startup_deadline=monotonic() + 15,
                        strict_reap=True,
                    ) as fixture,
                ):
                    require(
                        fixture("GET", "/api/credential") == {"data": []},
                        "Unexpected synthetic native credentials.",
                    )
                    workers = web_guest_worker_drain.AcceptedWorkers()
                    try:
                        _exercise(fixture, workers, evidence)
                    finally:
                        evidence["drained"] = workers.close(timeout=6)
            finally:
                os.chdir(cwd)
                evidence["native_closed"] = exit_info == {"code": 0, "forced": False}
            evidence["stage"] = "transport retirement"
            require(evidence["native_closed"], "Native fixture shutdown was not clean.")
            require(
                len(evidence["transport"]) == 3
                and all(all(result[1:]) for result in evidence["transport"])
                and evidence["drained"],
                "Native byte pump did not positively retire all transports and workers.",
            )
    except Exception as error:
        if type(error) is FixtureFailure:
            print(f"FAIL {evidence['stage']}: {error}", file=sys.stderr)
        else:
            print(f"FAIL {evidence['stage']}: verification failed.", file=sys.stderr)
        for stage, native_end, retired, reaped in evidence["transport"]:
            print(
                f"{stage}: native END={native_end}, CLOSED/EOF/reap={retired}, "
                f"clean exact reap={reaped}.",
                file=sys.stderr,
            )
        print(
            f"Checks: pairing headers/cookie={evidence.get('paired', False)}, "
            f"cookie identity={evidence.get('info', False)}, replay refusal={evidence.get('replay', False)}, "
            f"worker drain={evidence.get('drained', False)}, native shutdown={evidence.get('native_closed', False)}.",
            file=sys.stderr,
        )
        return 1
    print(
        "PASS OpenCode 2.4.1: native pairing redirect and cookie, exact /api/info, "
        "one-use replay refusal, orderly tunnel and worker retirement."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
