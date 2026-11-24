"""Compare the source-only client with Python's exact profile, not real OPAQUE."""

import base64
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from oc_box import (
    web_account_ingress as ingress,
    web_handoff,
    web_local_origins,
    web_pake_flow,
    web_pake_profile,
    web_pake_worker,
)

CLIENT_SOURCE = Path(__file__).resolve().parents[1] / "src/oc_box/web_pake_client.mjs"


def _wire(reply):
    return {
        "status": reply.status,
        "headers": dict(reply.headers),
        "body": reply.body.decode("ascii"),
    }


def _request(path, **fields):
    body = json.dumps(fields, separators=(",", ":")).encode("ascii")
    if ingress._inputs(path, body) != fields:
        raise AssertionError("Synthetic fields no longer match account ingress.")
    return body.decode("ascii")


def _vector(origins, username):
    account, workspace = origins.account.value, origins.workspace.value
    sizes = web_pake_profile.LocalPakeProfile.sizes
    ke1, ke2, ke3 = (
        bytes([value]) * size
        for value, size in ((251, sizes.ke1), (255, sizes.ke2), (252, sizes.ke3))
    )
    challenge = base64.urlsafe_b64encode(b"c" * 32).rstrip(b"=").decode("ascii")
    ticket = base64.urlsafe_b64encode(b"\0" * 7 + b"\1" + b"t" * 32)
    ticket = ticket.rstrip(b"=").decode("ascii")
    context = web_pake_flow._context(account, workspace, challenge)
    with patch.object(web_pake_worker, "PakeWorker") as worker:
        profile = web_pake_profile.LocalPakeProfile(
            42, expected_sha256=b"d" * 32, origins=origins
        )
        profile.start(
            setup=b"synthetic setup",
            credential_identifier=username.encode("ascii"),
            context=context,
            registration=None,
            ke1=ke1,
            deadline=123.5,
        )
        bindings = worker.return_value.start.call_args.kwargs
    b64 = web_pake_flow._b64
    start = web_pake_flow.PakeChallenge(challenge, b64(ke2), b64(context))
    finish = web_handoff.HandoffToken(ticket)
    return {
        "accountOrigin": account,
        "workspaceOrigin": workspace,
        "username": username,
        "ke1": b64(ke1),
        "ke2": b64(ke2),
        "ke3": b64(ke3),
        "context": b64(bindings["context"]),
        "clientIdentifier": b64(bindings["client_identifier"]),
        "verifierIdentifier": b64(bindings["verifier_identifier"]),
        "ticket": ticket,
        "startRequest": _request(ingress.START_PATH, username=username, ke1=b64(ke1)),
        "finishRequest": _request(
            ingress.FINISH_PATH, challenge_id=challenge, ke3=b64(ke3)
        ),
        "start": _wire(ingress._reply(ingress.START_PATH, start)),
        "finish": _wire(ingress._reply(ingress.FINISH_PATH, finish)),
    }


class BrowserPakeConformanceTests(unittest.TestCase):
    @unittest.skipUnless(
        CLIENT_SOURCE.is_file(), "Source checkout required for offline client"
    )
    @unittest.skipUnless(
        shutil.which("node"), "Node is required for offline client checks"
    )
    def test_exact_python_wire_and_browser_state_with_synthetic_session(self):
        vectors = [
            _vector(web_local_origins.LocalOrigins(), "sample-account"),
            _vector(web_local_origins.LocalOrigins(10443, 11444), "0"),
        ]
        result = subprocess.run(
            ["node", str(Path(__file__).with_name("web_pake_client.mjs"))],
            input=json.dumps(vectors),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Python profile exchange with synthetic session", result.stdout)
