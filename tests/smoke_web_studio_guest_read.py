"""Verify one-use Studio data on unmodified OpenCode, with only outer exec substituted."""

import argparse
from concurrent.futures import Future
from contextvars import ContextVar
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from unittest.mock import patch

from oc_box import (
    opencode_v2_io,
    web_guest_duplex_exec,
    web_guest_worker_drain as worker_drain,
    web_named_relay,
    web_studio_guest as guest,
    web_studio_guest_native as native,
    web_studio_guest_read as guest_read,
    web_studio_guest_selection as guest_selection,
)
from smoke_session_sync_v2 import require, seed
from smoke_web_studio_guest_native import _mint
from smoke_web_studio_guest_owner import _NATIVE_SHA, digest, exercise


_SESSION = "ses_guest_private_fixture"
_TITLE = "One-use fixture conversation"


def verify(binding, project):
    endpoint = binding.owner.owner.connection
    setup = opencode_v2_io.endpoint_request(endpoint)

    def safe_seed(method, path, value):
        require(
            method == "POST" and path == "/api/experimental/session/import",
            "Unexpected setup call.",
        )
        value["info"].pop("permissions")
        return setup(method, path, value)

    seed(safe_seed, _SESSION, project, title=_TITLE)
    original = binding.native.binding
    cookie = _mint(setup, original.authority)
    attempt = ContextVar("fixture_cookie_attempt", default=None)
    popen = subprocess.Popen

    def current(candidate, *, use, deadline):
        return (
            original
            if candidate is original
            and not use.cancelled
            and monotonic() < deadline
            and binding.current() is original
            else None
        )

    def cookie_verifier(candidate, **kw):
        return attempt.get()(candidate, **kw)

    admission = guest.GuestAdmission(
        original,
        observe_gateway=current,
        verify_native=cookie_verifier,
    )
    views = guest_selection.GuestSelections(admission, original)
    registry = worker_drain.AcceptedWorkers()

    def substitute(command, **kw):
        require(
            command[:9]
            == [
                "podman",
                "exec",
                "-i",
                "--workdir",
                "/",
                binding.identity.container_id,
                "/usr/bin/env",
                "-i",
                "PATH=/usr/bin:/bin",
            ]
            and command[9] in ("python3", "/usr/bin/python3")
            and command[10:14] == ["-I", "-S", "-B", "-c"]
            and "OPENCODE_PASSWORD" not in kw["env"]
            and cookie not in " ".join(command),
            "The original providerless relay command changed.",
        )
        return popen([sys.executable, *command[10:]], **kw)

    class OuterExec:
        Popen = staticmethod(substitute)

        def __getattr__(self, name):
            return getattr(subprocess, name)

    def verifier():
        return native.ManagedGuestCookieVerifier(
            binding.owner.owner.box, binding.native, observe_gateway=current
        )

    def confirm():
        body = admission.offer(original).value.encode("ascii")
        request = guest.ConfirmationRequest(
            "POST",
            guest.CONFIRM_PATH,
            (
                ("Host", original.authority),
                ("Origin", original.origin),
                ("Sec-Fetch-Site", "same-origin"),
                ("Sec-Fetch-Mode", "same-origin"),
                ("Sec-Fetch-Dest", "empty"),
                ("Content-Type", "text/plain"),
                ("Content-Length", str(len(body))),
                ("Cookie", cookie),
            ),
            body,
        )
        operation = verifier()
        token = attempt.set(operation)
        try:
            use, confirmed = admission.confirm(
                original,
                request,
                cancel=operation.cancel,
                deadline=monotonic() + 8,
            )
        finally:
            attempt.reset(token)
        require(
            confirmed is not None and operation.cleanup_confirmed,
            "Real guest confirmation refused.",
        )
        require(
            admission.finish(use, confirmed=True), "Guest confirmation did not retire."
        )
        return confirmed

    def query(page, operation):
        output = Future()
        left, right = socket.socketpair()
        with right:

            def work(worker_use):
                verification = verifier()
                try:
                    use, grant = admission.start(
                        original,
                        app=page.app,
                        page=page.page,
                        cancel=verification.cancel,
                        deadline=monotonic() + 8,
                    )
                    token = attempt.set(verification)
                    try:
                        value, closed = guest_read.read(
                            binding,
                            registry,
                            worker_use,
                            admission,
                            use,
                            grant,
                            views,
                            operation=operation,
                            cookie=cookie,
                        )
                    finally:
                        attempt.reset(token)
                    output.set_result(
                        (use, value, closed and verification.cleanup_confirmed)
                    )
                except BaseException as error:
                    output.set_exception(error)

            if not registry.dispatch(left, work):
                left.close()
                require(False, "Read worker was not registered.")
            use, result, closed = output.result(timeout=12)
            right.settimeout(4)
            require(
                right.recv(1) == b"", "Read worker did not retire its local socket."
            )
            require(
                admission.finish(use, confirmed=closed) and closed,
                "Guest read did not retire.",
            )
            return result

    verified = False
    try:
        # Substitute only the outer exec entrypoints. DuplexHost still sees
        # the genuine Popen type and verifies the exact real pipes and reap.
        with (
            patch.object(web_guest_duplex_exec, "subprocess", OuterExec()),
            patch.object(web_named_relay, "subprocess", OuterExec()),
        ):
            page = confirm()
            catalog = query(page, guest_read.Catalog())
            require(
                catalog is not None
                and catalog["sessions"]
                == [
                    {"id": _SESSION, "title": _TITLE, "archived": False, "child": False}
                ],
                "Guest catalog did not select the native fixture workspace.",
            )
            view = query(page, guest_read.Select(1, _SESSION))
            require(
                view == {"generation": 1, "sessionID": _SESSION},
                "Guest selection refused.",
            )
            history = query(page, guest_read.Transcript(_SESSION, 1))
            require(
                history is not None
                and history["sessionID"] == _SESSION
                and len(history["messages"]) == 1
                and history["messages"][0]["text"] == "SYNTHETIC_" + _SESSION,
                "Guest transcript did not match the retained native message.",
            )
            verified = True
    finally:
        admission.close()
        views.close()
        workers_drained = registry.close(timeout=5)
        admission_drained = admission.wait_drained(2)
        if verified:
            require(
                workers_drained and admission_drained,
                "Guest read lifetime did not drain.",
            )
        elif not (workers_drained and admission_drained):
            print("Guest read fixture cleanup could not be verified.", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", type=Path, required=True)
    args = parser.parse_args()
    if os.getuid() == 0 or os.environ.get("OC_BOX_CONTAINER") == "1":
        parser.error("Use a non-root private synthetic native test environment.")
    try:
        binary = args.opencode.resolve(strict=True)
        require(digest(binary) == _NATIVE_SHA, "Unexpected native artifact.")
        with TemporaryDirectory(prefix="ocb-studio-guest-read-") as directory:
            exercise(Path(directory), binary, verify_binding=verify)
    except Exception as error:
        from smoke_session_sync_v2 import FixtureFailure

        if type(error) is FixtureFailure:
            print(str(error), file=sys.stderr)
        else:
            print(f"FAIL guest read fixture: {type(error).__name__}.", file=sys.stderr)
        return 1
    print(
        "PASS OpenCode 2.4.1: original guest grant, catalog, selection, and exact transcript."
    )
    print(
        "Synthetic outer inspect/exec; no browser routes, Podman, or production data."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
