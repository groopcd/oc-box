"""Provider-free registered relay tests; the fake exec is local and unserved."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from time import monotonic
import unittest
from unittest.mock import patch

from oc_box import oc_box as box, web_studio_guest as guest, workspace_service
from oc_box import web_studio_guest_native as native


ORIGIN = "http://127.0.0.1:49152"
ENDPOINT = "http://127.0.0.1:49153"
COOKIE = b"synthetic=opaque-cookie-not-verified-by-shape"
CONTAINER = "c" * 64
PID = 492
INFO = {
    "version": "2.4.1",
    "pid": PID,
    "urls": [ENDPOINT],
    "hostname": "synthetic",
    "paths": {"tmp": "/synthetic/tmp"},
    "capabilities": {"persistentPty": True},
}
UNAUTHORIZED = {"_tag": "UnauthorizedError", "message": "Authentication required"}
FAKE_EXEC = """
import os, sys
request = b''
while b'\\r\\n\\r\\n' not in request:
    part = os.read(0, 4096)
    if not part:
        sys.exit(0)
    request += part
    if len(request) > 8192:
        sys.exit(1)
assert request == request.partition(b'\\r\\n\\r\\n')[0] + b'\\r\\n\\r\\n'
assert request.startswith(b'GET /api/info HTTP/1.1\\r\\nhost: 127.0.0.1:49152\\r\\n'
                          b'origin: http://127.0.0.1:49152\\r\\n')
assert b'authorization:' not in request.lower()
assert (b'\\r\\ncookie: ' in request) == (sys.argv[1] == 'yes')
assert request.endswith(b'Accept: application/json\\r\\nAccept-Encoding: identity'
                        b'\\r\\nConnection: close\\r\\n\\r\\n')
if sys.argv[3] == 'cancel':
    while os.read(0, 4096):
        pass
else:
    os.write(1, bytes.fromhex(sys.argv[2]))
    sys.exit(int(sys.argv[3]))
"""


def reply(status, value, *, origin=ORIGIN, extra=b""):
    body = json.dumps(value, separators=(",", ":")).encode()
    challenge = (
        b'www-authenticate: Basic realm="Secure Area"\r\n' if status == 401 else b""
    )
    return (
        (
            f"HTTP/1.1 {status} Fixture\r\nConnection: close\r\nContent-Type: application/json\r\n"
            f"Access-Control-Allow-Origin: {origin}\r\nContent-Length: {len(body)}\r\n"
        ).encode()
        + challenge
        + extra
        + b"\r\n"
        + body
    )


class ManagedGuestCookieTests(unittest.TestCase):
    def setUp(self):
        process_environment = patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"})
        process_environment.start()
        self.addCleanup(process_environment.stop)
        self.binding = guest.GuestBinding("a" * 32, "b" * 32, CONTAINER, ORIGIN)
        self.scope = native.GuestNativeScope(self.binding, ENDPOINT, PID, "2.4.1")
        self.deadline = monotonic() + 8
        self.use = guest.GuestUse(self.deadline)
        self.head = (
            native._LINE
            + b"\r\n"
            + native._host_origin(self.binding)
            + b"\r\ncookie: "
            + COOKIE
        )
        self.normal_challenge = (False, reply(401, UNAUTHORIZED), "0")

    def attempt(self, observer=None):
        return native.ManagedGuestCookieVerifier(
            box, self.scope, observe_gateway=observer or (lambda binding, **kw: binding)
        )

    def check(self, verifier, **overrides):
        args = {
            "use": self.use,
            "headers": self.head,
            "authority": self.binding.authority,
            "deadline": self.deadline,
        }
        return verifier(self.binding, **(args | overrides))

    def local_exec(self, rows, *, spawned=None):
        spawn = subprocess.Popen
        expected = list(rows)
        children = []

        def exact(command, **options):
            self.assertEqual(
                command,
                [
                    "podman",
                    "exec",
                    "-i",
                    "--workdir",
                    "/",
                    CONTAINER,
                    "/usr/bin/env",
                    "-i",
                    "PATH=/usr/bin:/bin",
                    "python3",
                    "-I",
                    "-S",
                    "-B",
                    "-c",
                    workspace_service.PROXY,
                    "49153",
                ],
            )
            self.assertIs(options["stdin"], options["stdout"])
            self.assertEqual(options["stderr"], subprocess.DEVNULL)
            self.assertEqual(options["cwd"], "/")
            for value in [*command, *options["env"].values()]:
                self.assertNotIn(COOKIE, os.fsencode(value))
            auth, raw, code = expected[len(children)]
            process = spawn(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-B",
                    "-c",
                    FAKE_EXEC,
                    "yes" if auth else "no",
                    raw.hex(),
                    code,
                ],
                **options,
            )
            children.append(process)
            if spawned is not None:
                spawned.set()
            return process

        return patch.object(subprocess, "Popen", side_effect=exact), children

    def assert_reaped(self, children, *codes):
        self.assertEqual(tuple(child.returncode for child in children), codes)
        for child in children:
            with self.assertRaises(ChildProcessError):
                os.waitpid(child.pid, os.WNOHANG)

    def test_exact_native_scope_and_header_block_refuse_before_launch(self):
        for fields in (
            {"version": "2.3.2"},
            {"version": "2.4.1-rc.1"},
            {"native_pid": True},
            {"native_pid": 0},
            {"endpoint": ENDPOINT + "/"},
            {"endpoint": ORIGIN + "?x"},
        ):
            with (
                self.subTest(fields=fields),
                self.assertRaises(native.GuestNativeError),
            ):
                replace(self.scope, **fields)
        with patch.object(subprocess, "Popen") as start:
            for headers in (
                self.head.replace(b"GET /api/info ", b"GET / "),
                self.head.replace(
                    b"origin: " + ORIGIN.encode(), b"origin: http://127.0.0.1:49154"
                ),
                self.head.replace(
                    b"host: " + self.binding.authority.encode(), b"host: localhost"
                ),
                self.head.rsplit(b"\r\n", 1)[0],
                self.head + b"\r\nAuthorization: Basic no",
                self.head + b"\r\ncookie: second=value",
                self.head + b"\r\n\r\n",
                self.head + b"x" * 4097,
            ):
                attempt = self.attempt()
                self.assertIsNone(self.check(attempt, headers=headers))
                self.assertTrue(attempt.cleanup_confirmed)
            self.assertIsNone(self.check(self.attempt(), authority="localhost:49152"))
            start.assert_not_called()

    def test_cookie_verification_is_one_use_and_reaps_both_exact_children(self):
        hook, children = self.local_exec(
            [self.normal_challenge, (True, reply(200, INFO), "0")]
        )
        attempt = self.attempt()
        with hook:
            self.assertIs(self.check(attempt), self.binding)
            self.assertIsNone(self.check(attempt))
        self.assertTrue(attempt.cleanup_confirmed)
        self.assert_reaped(children, 0, 0)

    def test_public_info_is_refused_before_sending_cookie_even_with_perfect_identity(
        self,
    ):
        hook, children = self.local_exec([(False, reply(200, INFO), "0")])
        attempt = self.attempt()
        with hook:
            self.assertIsNone(self.check(attempt))
        self.assertTrue(attempt.cleanup_confirmed)
        self.assert_reaped(children, 0)

    def test_bad_native_identity_and_ambiguous_responses_never_authorize(self):
        for name, response in (
            ("pid", reply(200, INFO | {"pid": PID + 1})),
            ("version", reply(200, INFO | {"version": "2.4.2"})),
            ("endpoint", reply(200, INFO | {"urls": [ORIGIN]})),
            ("no identity", reply(200, {})),
            ("refused", reply(401, UNAUTHORIZED)),
            ("redirect", reply(302, INFO, extra=b"Location: /\r\n")),
            ("duplicate length", reply(200, INFO, extra=b"Content-Length: 1\r\n")),
            (
                "native cookie",
                reply(200, INFO, extra=b"Set-Cookie: synthetic=ignored\r\n"),
            ),
            ("other origin", reply(200, INFO, origin="http://127.0.0.1:49155")),
            ("pipelined", reply(200, INFO) + reply(200, INFO)),
        ):
            with self.subTest(case=name):
                hook, children = self.local_exec(
                    [self.normal_challenge, (True, response, "0")]
                )
                attempt = self.attempt()
                with hook:
                    self.assertIsNone(self.check(attempt))
                self.assertTrue(attempt.cleanup_confirmed)
                self.assert_reaped(children, 0, 0)

    def test_owner_change_before_launch_and_precancel_never_spawn(self):
        lookalike = replace(self.binding)
        with patch.object(subprocess, "Popen") as start:
            lost = self.attempt(lambda binding, **kw: lookalike)
            self.assertIsNone(self.check(lost))
            self.assertTrue(lost.cleanup_confirmed)
            cancelled = self.attempt()
            cancelled.cancel()
            self.assertIsNone(self.check(cancelled))
            self.assertTrue(cancelled.cleanup_confirmed)
            start.assert_not_called()

    def test_cancellation_between_probes_prevents_the_credential_bearing_launch(self):
        hook, children = self.local_exec([self.normal_challenge])
        attempt = self.attempt()
        parse = native._reply

        def then_cancel(raw, scope, *, authenticated):
            parse(raw, scope, authenticated=authenticated)
            attempt.cancel()

        with hook, patch.object(native, "_reply", side_effect=then_cancel):
            self.assertIsNone(self.check(attempt))
        self.assertTrue(attempt.cleanup_confirmed)
        self.assert_reaped(children, 0)

    def test_spawn_ambiguity_is_not_confirmed_cleanup(self):
        attempt = self.attempt()
        with patch.object(
            subprocess, "Popen", side_effect=OSError("private detail")
        ) as start:
            self.assertIsNone(self.check(attempt))
            self.assertIsNone(self.check(attempt))
        start.assert_called_once()
        self.assertFalse(attempt.cleanup_confirmed)

    def test_native_response_eof_does_not_authorize_unconfirmed_process_result(self):
        hook, children = self.local_exec(
            [self.normal_challenge, (True, reply(200, INFO), "7")]
        )
        attempt = self.attempt()
        with hook:
            self.assertIsNone(self.check(attempt))
        self.assertFalse(attempt.cleanup_confirmed)
        self.assert_reaped(children, 0, 7)

    def test_cancellation_after_managed_start_cannot_turn_local_shutdown_into_eof(self):
        spawned = Event()
        hook, children = self.local_exec([(False, b"", "cancel")], spawned=spawned)
        attempt = self.attempt()
        with hook, ThreadPoolExecutor(max_workers=1) as worker:
            result = worker.submit(self.check, attempt)
            try:
                self.assertTrue(spawned.wait(3))
            finally:
                attempt.cancel()
            self.assertIsNone(result.result(timeout=3))
        # A local shutdown is not EOF evidence even if it let the worker exit.
        self.assertFalse(attempt.cleanup_confirmed)
        self.assert_reaped(children, 0)


if __name__ == "__main__":
    unittest.main()
