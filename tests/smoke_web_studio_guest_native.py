"""Verify the guest cookie seam against pinned unmodified OpenCode 2.4.1.

Only the managed exec hop is substituted. No oc-box route, real workspace,
provider or host gateway is started.
"""

import argparse
from contextvars import ContextVar
from hashlib import sha256
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
from smoke_session_sync_v2 import bounded, private_environment, require

from oc_box import oc_box as box, opencode_compat, opencode_v2_io, workspace_service
from oc_box import web_native_pair as pair, web_studio_guest as guest
from oc_box import web_studio_guest_native as native


_SHA256 = "ec93043f18f8a6b4d8e71ec9260e292ba6f91b66b142782135d40f0b58b0c152"
_CONTAINER = "c" * 64


def _pin(binary, env, project):
    digest = sha256()
    with open(binary, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    require(
        digest.hexdigest() == _SHA256,
        "The fixture requires the pinned Linux x64 binary.",
    )
    require(
        opencode_compat.version(bounded([binary, "--version"], env, project, timeout=8))
        == "2.4.1",
        "The supplied native version did not match the pinned build.",
    )


def _native_wire(
    endpoint, authority, target, *, origin=None, cookie=None, head_only=False
):
    origin = "http://" + authority if origin is None else origin
    fields = [
        f"GET {target} HTTP/1.1",
        "Host: " + authority,
        "Origin: " + origin,
        "Accept: text/html"
        if head_only or target.startswith("/auth/")
        else "Accept: application/json",
        "Accept-Encoding: identity",
        "Connection: close",
    ]
    if cookie is not None:
        fields.append("Cookie: " + cookie)
    deadline = monotonic() + 4
    raw = bytearray()
    port = pair._port(endpoint)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as peer:
        peer.settimeout(4)
        peer.connect(("127.0.0.1", port))
        peer.sendall(("\r\n".join(fields) + "\r\n\r\n").encode("ascii"))
        while True:
            left = deadline - monotonic()
            require(
                left > 0 and len(raw) < 16500, "Synthetic native wire bound exceeded."
            )
            peer.settimeout(left)
            part = peer.recv(min(4096, 16500 - len(raw)))
            if not part:
                return pair._reply(bytes(raw))
            raw.extend(part)
            if head_only and b"\r\n\r\n" in raw:
                return pair._reply(bytes(raw.partition(b"\r\n\r\n")[0]) + b"\r\n\r\n")


def _mint(fixture, authority):
    # Native setup alone uses the synthetic host Basic credential. Neither
    # runtime verifier request, worker environment nor fixture output does.
    code = fixture("POST", "/api/pair")
    require(
        set(code) == {"code", "expires_in"}
        and code["expires_in"] == 300
        and pair._CODE.fullmatch(code["code"]),
        "Unexpected native pairing response.",
    )
    target = "/auth/connect/" + code["code"]
    args = fixture.endpoint["url"], authority, target
    value = pair._parse_cookie(_native_wire(*args), authority)
    require(
        _native_wire(*args)[0] == 401, "Native one-use pairing replay was not refused."
    )
    return pair._cookie_name(authority) + "=" + value


def _exercise(fixture):
    endpoint = fixture.endpoint["url"]
    port = pair._port(endpoint)
    # Deliberately different native TCP and browser authority ports. The
    # latter is synthetic: no second listener or legacy Gateway is needed.
    ports = [candidate for candidate in (49152, 49153, 49154) if candidate != port]
    original, foreign = (f"127.0.0.1:{selected}" for selected in ports[:2])
    binding = guest.GuestBinding("a" * 32, "b" * 32, _CONTAINER, "http://" + original)
    identify = pair.named_native_info(fixture("GET", "/api/info"), "2.4.1", endpoint)
    scope = native.GuestNativeScope(binding, endpoint, identify["pid"], "2.4.1")
    cookie, foreign_cookie = _mint(fixture, original), _mint(fixture, foreign)
    invalid_cookie = pair._cookie_name(original) + "=synthetic-invalid"
    for fields in (
        {},
        {"cookie": invalid_cookie},
        {"cookie": foreign_cookie},
        {"cookie": cookie, "origin": "http://" + foreign},
    ):
        require(
            _native_wire(endpoint, original, "/api/info", **fields)[0] == 401,
            "Native cookie denial, authority isolation or origin behavior changed.",
        )
    require(
        _native_wire(endpoint, original, "/", head_only=True)[0] == 200,
        "The positive public-route control did not return 200.",
    )

    def observe(candidate, *, use, deadline):
        return (
            binding
            if candidate is binding and not use.cancelled and monotonic() < deadline
            else None
        )

    pending = ContextVar("fixture_request_verifier", default=None)

    def dispatch(candidate, **arguments):
        attempt = pending.get()
        return attempt(candidate, **arguments) if attempt is not None else None

    admission = guest.GuestAdmission(
        binding, observe_gateway=observe, verify_native=dispatch
    )
    spawn = subprocess.Popen
    children = []

    def local_popen(command, **options):
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
                str(port),
            ]
            and options["stdin"] is options["stdout"]
            and options["stderr"] == subprocess.DEVNULL
            and options["cwd"] == "/"
            and all(
                value not in "".join([*command, *options["env"].values()])
                for value in (cookie, foreign_cookie, fixture.endpoint["password"])
            )
            and "OPENCODE_PASSWORD" not in options["env"],
            "The verifier did not use the fixed private managed exec.",
        )
        child = spawn([sys.executable, *command[-6:]], **options)
        children.append(child)
        return child

    def confirm(value, *, origin=binding.origin, return_bootstrap=False):
        body = admission.offer(binding).value.encode("ascii")
        request = guest.ConfirmationRequest(
            "POST",
            guest.CONFIRM_PATH,
            (
                ("Host", binding.authority),
                ("Origin", origin),
                ("Sec-Fetch-Site", "same-origin"),
                ("Sec-Fetch-Mode", "same-origin"),
                ("Sec-Fetch-Dest", "empty"),
                ("Content-Length", str(len(body))),
                ("Content-Type", "text/plain"),
                *(() if value is None else (("Cookie", value),)),
            ),
            body,
        )
        attempt = native.ManagedGuestCookieVerifier(box, scope, observe_gateway=observe)
        token = pending.set(attempt)
        try:
            result = admission.confirm(
                binding, request, cancel=attempt.cancel, deadline=monotonic() + 8
            )
        finally:
            pending.reset(token)
        require(
            attempt.cleanup_confirmed,
            "Managed fixture transport retirement was unconfirmed.",
        )
        if result is None:
            return None
        use, bootstrap = result
        require(
            admission.finish(use, confirmed=attempt.cleanup_confirmed),
            "Guest fixture use could not be retired.",
        )
        return bootstrap if return_bootstrap else bootstrap is not None

    try:
        with patch.object(subprocess, "Popen", side_effect=local_popen):
            require(
                confirm(None) is None
                and confirm(cookie, origin="http://" + foreign) is None
                and not children,
                "Missing or cross-origin credentials reached managed exec.",
            )
            require(
                confirm(invalid_cookie) is False and confirm(foreign_cookie) is False,
                "Unverified native cookies were admitted.",
            )
            require(
                confirm(cookie) is True,
                "The genuine same-origin pairing cookie was not admitted.",
            )
            # The public path is disallowed even if it is submitted in the
            # standalone verifier seam with an otherwise genuine cookie.
            forwarded = (
                b"GET / HTTP/1.1\r\n"
                + native._host_origin(binding)
                + b"\r\ncookie: "
                + cookie.encode("ascii")
            )
            deadline = monotonic() + 3
            attempt = native.ManagedGuestCookieVerifier(
                box, scope, observe_gateway=observe
            )
            require(
                attempt(
                    binding,
                    use=guest.GuestUse(deadline),
                    headers=forwarded,
                    authority=original,
                    deadline=deadline,
                )
                is None
                and len(children) == 6,
                "A public route reached the native verifier transport.",
            )
            bootstrap = confirm(cookie, return_bootstrap=True)
            require(bootstrap is not None, "Synthetic page confirmation refused.")

            def authorize(value):
                attempt = native.ManagedGuestCookieVerifier(
                    box, scope, observe_gateway=observe
                )
                registered = admission.start(
                    binding,
                    app=bootstrap.app,
                    page=bootstrap.page,
                    cancel=attempt.cancel,
                    deadline=monotonic() + 8,
                )
                if registered is None:
                    return None
                use, grant = registered
                require(
                    grant is bootstrap.grant and not admission.native_current(use),
                    "The guest app/page pair incorrectly authorized native work.",
                )
                token = pending.set(attempt)
                try:
                    accepted = admission.authorize_native(use, grant, cookie=value)
                finally:
                    pending.reset(token)
                require(
                    admission.native_current(use) is accepted
                    and attempt.cleanup_confirmed
                    and admission.finish(use, confirmed=True),
                    "Native continuity or managed retirement was unconfirmed.",
                )
                return accepted

            require(
                authorize(cookie) is True
                and authorize(None) is False
                and authorize(cookie) is None
                and len(children) == 10,
                "An authenticated page survived cookie removal or was revived.",
            )
        require(
            all(child.returncode == 0 for child in children),
            "Unclean managed fixture exec.",
        )
        for child in children:
            try:
                os.waitpid(child.pid, os.WNOHANG)
            except ChildProcessError:
                continue
            require(False, "A managed fixture exec was not previously reaped.")
    finally:
        admission.close()
        # This is bounded exact-child cleanup in a synthetic fixture only.
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)
    require(admission.wait_drained(0), "Guest fixture admission did not drain.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", type=Path, required=True)
    options = parser.parse_args()
    try:
        binary = str(options.opencode.resolve(strict=True))
        with tempfile.TemporaryDirectory(prefix="studio-guest-native-") as directory:
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
            cwd, exit_info = os.getcwd(), {}
            try:
                os.chdir(project)
                with (
                    patch.dict(os.environ, env, clear=True),
                    opencode_v2_io.server(
                        binary, exit_info=exit_info, startup_deadline=monotonic() + 15
                    ) as fixture,
                ):
                    require(
                        fixture("GET", "/api/credential") == {"data": []},
                        "Unexpected synthetic native credentials.",
                    )
                    _exercise(fixture)
            finally:
                os.chdir(cwd)
            require(
                exit_info == {"code": 0, "forced": False},
                "Native fixture shutdown was not clean.",
            )
    except Exception as error:
        # Only known fixed fixture messages, never native/subprocess output.
        from smoke_session_sync_v2 import FixtureFailure

        if type(error) is FixtureFailure:
            print(str(error), file=sys.stderr)
        else:
            print("Studio guest native fixture could not be verified.", file=sys.stderr)
        return 1
    print(
        "PASS OpenCode 2.4.1: paired cookie, missing/invalid/foreign refusal, public path refusal, managed reap."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
