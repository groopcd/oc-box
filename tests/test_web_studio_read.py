from dataclasses import replace
import json
import os
import socket
import sys
from time import monotonic
import unittest
from unittest.mock import Mock, patch

from oc_box import oc_box as box, web_admission, web_named_owner, web_named_relay
from oc_box import web_studio_conversation as studio
from oc_box import web_studio_read as read


SESSION_ID = "ses_studio_read"
PASSWORD = "synthetic_native_" + "p" * 24
VALUE = {
    "sessionID": SESSION_ID,
    "title": "Studio synthetic",
    "messages": [
        {
            "id": "msg_studio",
            "role": "assistant",
            "text": "<script>literal text</script>",
            "truncated": False,
        }
    ],
    "hasEarlier": False,
}


def emit(value=VALUE):
    return (
        f"sys.stdout.buffer.write({studio._READ_READY + json.dumps(value).encode()!r})"
    )


class StudioReadTests(unittest.TestCase):
    def apply_patch(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()

    def setUp(self):
        self.apply_patch(patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}))
        control, self.peer = socket.socketpair()
        self.addCleanup(control.close)
        self.addCleanup(self.peer.close)
        self.owner = web_named_owner.OwnerIdentity(
            "a" * 32, "b" * 32, "c" * 64, "2.4.1", "http://127.0.0.1:49153"
        )
        self.live_owner = self.owner
        self.binding = web_admission.WebBinding(
            self.owner.workspace_id,
            self.owner.generation,
            "https://workspace.localhost",
        )
        self.admission = web_admission.BrowserAdmission(self.binding, max_connections=1)
        self.browser = self.issue()
        self.scope = read.ReadScope(self.owner, self.browser, SESSION_ID, 9)
        self.live_scope = self.scope
        self.request = read.ReadRequest(
            "GET", read.PATH + "?session=" + SESSION_ID, b""
        )
        self.lease = web_named_owner.VerifiedOwnerLease(
            self.owner,
            control,
            "/synthetic/project",
            "/synthetic/home",
            PASSWORD,
            observe=lambda: self.live_owner,
            connect_native=Mock(side_effect=AssertionError("not a native relay")),
        )
        self.write = Mock()
        self.interrupt = Mock()
        self.retire = Mock(return_value=True)
        self.command = self.apply_patch(
            patch.object(box.workspace_service, "client_command")
        )
        self.script(emit())

    def issue(self):
        token = self.admission.issue(self.binding, principal="synthetic")
        return self.admission.lookup(self.binding, token)

    def script(self, script, *, startup="assert os.read(0,1)==b'R'"):
        self.command.return_value = box.ContainerCommand(
            [sys.executable, "-I", "-S", "-c", f"import os,sys; {startup}; {script}"],
            box.maintenance_host_env(),
        )

    def respond(self, **overrides):
        args = {
            "request": self.request,
            "scope": self.scope,
            "lease": self.lease,
            "admission": self.admission,
            "binding": self.binding,
            "observe": lambda _: self.live_scope,
            "deadline": monotonic() + 2,
            "write": self.write,
            "interrupt": self.interrupt,
            "retire": self.retire,
            **overrides,
        }
        return read.respond(box, **args)

    def refused(self, **kwargs):
        with self.assertRaisesRegex(
            read.ReadError, r"^The Studio conversation could not be verified\.$"
        ):
            self.respond(**kwargs)

    def test_explicit_envelope_under_actual_grant_and_immutable_worker_target(self):
        def verify(reply, deadline, *, check):
            check()
            self.assertIs(type(reply), read.ReadResponse)
            self.assertEqual(reply.status, 200)
            self.assertEqual(json.loads(reply.body), VALUE)
            self.assertEqual(dict(reply.headers)["Cache-Control"], "no-store")
            self.assertNotIn(PASSWORD, repr(reply))
            self.assertNotIn(self.browser._token.encode(), reply.body)
            self.assertLessEqual(deadline, self.browser.expires_at)
            self.retire.assert_not_called()
            with self.admission.connection(
                self.binding, self.browser, cancel=lambda: None
            ) as use:
                self.assertIsNone(use)  # Response delivery still charges this request.

        self.write.side_effect = verify
        self.assertIsNone(self.respond())
        self.write.assert_called_once()
        self.retire.assert_called_once()
        _, data, connection, argv = self.command.call_args.args
        self.assertEqual(data["container"], self.owner.container_id)
        self.assertEqual(
            argv[-4:],
            [studio._MANAGED_ARG, self.owner.endpoint, self.lease.project, SESSION_ID],
        )
        self.assertNotIn(PASSWORD, " ".join(argv))
        self.assertEqual(set(connection), {"workspace", "version", "url", "password"})
        self.lease.connect_native.assert_not_called()
        self.assertTrue(
            self.admission.begin_revocation(self.binding, self.browser).wait_drained(0)
        )

    def test_missing_scope_or_exact_envelope_refuses_without_worker(self):
        for overrides in (
            {"scope": None},
            {"admission": None},  # Native guest auth, alone, is not registered use.
            {"binding": replace(self.binding, owner_generation="e" * 32)},
            {
                "lease": replace(
                    self.lease, identity=replace(self.owner, container_id="d" * 64)
                )
            },
            {"scope": replace(self.scope, browser=replace(self.browser))},
            {"deadline": float("inf")},
            {"deadline": monotonic() - 1},
            {"request": replace(self.request, method="POST")},
            {"request": replace(self.request, body=b"{}")},
            {"request": replace(self.request, target=read.PATH)},
            {"request": replace(self.request, target=read.PATH + "?session=ses_other")},
            {
                "request": replace(
                    self.request, target=read.PATH + "?session=%73es_studio_read"
                )
            },
            {
                "request": replace(
                    self.request, target=self.request.target + "&cursor=opaque"
                )
            },
            {
                "request": replace(
                    self.request, target=self.request.target + "&session=" + SESSION_ID
                )
            },
            {"request": replace(self.request, target="/api/session/" + SESSION_ID)},
        ):
            with self.subTest(fields=tuple(overrides)):
                self.refused(**overrides)
        self.command.assert_not_called()
        self.write.assert_not_called()

    def test_owner_lost_unverified_control_and_unsupported_version_refuse(self):
        with self.assertRaises(read.ReadError):
            replace(self.scope, owner=replace(self.owner, version="2.3.2"))
        self.live_owner = replace(self.owner, generation="e" * 32)
        self.refused()
        self.live_owner = self.owner
        self.peer.sendall(b"unexpected control bytes")
        self.refused()
        self.command.assert_not_called()
        self.write.assert_not_called()

    def test_exact_owner_grant_and_selection_rechecked_after_worker(self):
        original = studio._display_value
        for name, changes in (
            ("other ID", {"live_scope": replace(self.scope, session_id="ses_other")}),
            (
                "away and back",
                {"live_scope": replace(self.scope, selection_generation=11)},
            ),
            (
                "other browser",
                {"live_scope": replace(self.scope, browser=self.issue())},
            ),
            (
                "same endpoint, new owner generation",
                {"live_owner": replace(self.owner, generation="e" * 32)},
            ),
        ):

            def read_then_change(*args):
                result = original(*args)
                self.__dict__.update(changes)
                return result

            with (
                self.subTest(name=name),
                patch.object(studio, "_display_value", read_then_change),
            ):
                self.live_owner, self.live_scope = self.owner, self.scope
                self.refused()
        self.assertEqual(self.command.call_count, 4)
        self.write.assert_not_called()

    def test_writer_gets_rechecks_and_revocation_waits_for_delivery(self):
        revoked = []
        drained_during_write = []

        def revoke(reply, deadline, *, check):
            check()
            revoked.append(self.admission.begin_revocation(self.binding, self.browser))
            drained_during_write.append(revoked[0].wait_drained(0))
            check()

        self.write.side_effect = revoke
        self.refused()
        self.write.assert_called_once()
        self.interrupt.assert_called_once()
        self.assertEqual(drained_during_write, [False])
        self.assertTrue(revoked[0].wait_drained(0))

    def test_revocation_during_startup_withholds_gate_and_drains(self):
        self.script("sys.stdout.buffer.write(b'N')", startup="assert os.read(0,1)==b''")
        actual_start = web_named_relay._WorkspaceChild.start
        revoked = []
        drained_at_start = []

        def revoke(child, command, stdio):
            actual_start(child, command, stdio)
            revoked.append(self.admission.begin_revocation(self.binding, self.browser))
            drained_at_start.append(revoked[0].wait_drained(0))

        with patch.object(web_named_relay._WorkspaceChild, "start", revoke):
            self.refused()
        self.assertEqual(drained_at_start, [False])
        self.assertTrue(revoked[0].wait_drained(0))
        self.interrupt.assert_called_once()
        self.write.assert_not_called()

    def test_deadline_closes_worker_gate_and_requires_terminal_evidence(self):
        self.script("assert os.read(0,1)==b''; sys.stdout.buffer.write(b'N')")
        self.refused(deadline=monotonic() + 0.3)
        self.write.assert_not_called()
        self.assertTrue(
            self.admission.begin_revocation(self.binding, self.browser).wait_drained(0)
        )

    def test_clean_refusal_or_invalid_projection_never_reaches_writer(self):
        for script in (
            "sys.stdout.buffer.write(b'N')",
            emit({**VALUE, "nativeHeaders": [PASSWORD]}),
            emit({**VALUE, "sessionID": "ses_other"}),
            emit({**VALUE, "title": PASSWORD}),
        ):
            self.script(script)
            self.refused()
        self.write.assert_not_called()
        self.assertTrue(
            self.admission.begin_revocation(self.binding, self.browser).wait_drained(0)
        )

    def test_incomplete_worker_fences_site_and_does_not_claim_grant_drain(self):
        self.script(
            "sys.stdout.buffer.write(b'Y{}'); sys.exit(1)", startup="os.read(0,1)"
        )
        actual_start = web_named_relay._WorkspaceChild.start
        revoked = []

        def revoke(child, command, stdio):
            actual_start(child, command, stdio)
            revoked.append(self.admission.begin_revocation(self.binding, self.browser))

        with patch.object(web_named_relay._WorkspaceChild, "start", revoke):
            self.refused()
        self.write.assert_not_called()
        self.assertFalse(revoked[0].wait_drained(0))
        self.assertFalse(self.admission.wait_drained(0))
        self.assertIsNone(self.admission.lookup(self.binding, self.browser._token))
        self.assertIsNone(self.issue())
        self.refused()
        self.command.assert_called_once()

    def test_downstream_retirement_is_required_even_after_a_clean_worker(self):
        self.script("sys.stdout.buffer.write(b'N')")
        self.retire.return_value = None
        self.refused()
        self.write.assert_not_called()
        self.retire.assert_called_once()
        self.assertFalse(self.admission.wait_drained(0))


if __name__ == "__main__":
    unittest.main()
