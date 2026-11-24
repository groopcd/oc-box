import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import attachment_io, attachments, oc_box as box, sharing, workspace_service

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
SESSION = "ses_attachment_fixture"
OTHER = "ses_other_fixture"
REAL_NATIVE = attachments.native


class AttachmentEligibilityTests(unittest.TestCase):
    def test_broker_check_verifies_exact_version_and_target_without_prompt_route(self):
        release = "2.99.0"
        request = Mock(
            side_effect=[
                {"version": release},
                {
                    "data": {
                        "id": SESSION,
                        "location": {"directory": "/synthetic/project"},
                        "time": {},
                    }
                },
            ]
        )
        value = {
            "operation": "broker-check",
            "version": release,
            "session": SESSION,
            "directory": "/synthetic/project",
        }
        self.assertEqual(attachment_io.deliver(request, value), {"ok": True})
        self.assertEqual(
            [call.args for call in request.call_args_list],
            [("GET", "/api/info"), ("GET", "/api/session/" + SESSION)],
        )
        request.reset_mock(side_effect=True)
        request.return_value = {"version": "2.99.1"}
        with self.assertRaisesRegex(attachment_io.Refused, "^native-version$"):
            attachment_io.deliver(request, value)
        request.assert_called_once_with("GET", "/api/info")
        for change in (
            {"operation": "check"},
            {"operation": "send"},
            {"prompt": {}},
            {"version": "2.099.0"},
            {"version": "2.99.0-rc.1"},
        ):
            request.reset_mock()
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(attachment_io.Refused, "^invalid-request$"),
            ):
                attachment_io.deliver(request, {**value, **change})
            request.assert_not_called()


class AttachmentBrokerTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.path = self.root / "Screenshot.png"
        self.path.write_bytes(PNG)
        self.now = 100.0
        self.broker = attachments.Broker(clock=lambda: self.now)
        self.addCleanup(self.broker.close)

    def offer(self, **kwargs):
        return self.broker.offer(
            self.path, SESSION, consent=kwargs.get("consent", lambda _: True)
        )

    def test_selected_file_becomes_session_bound_one_use_snapshot_not_live_share(self):
        consent = Mock(return_value=True)
        token = self.offer(consent=consent)
        consent.assert_called_once_with(self.path)
        self.assertNotIn(str(self.path), token)
        self.path.unlink()
        self.path.write_bytes(b"not the authorized image")
        with self.assertRaises(attachments.AttachmentError):
            self.broker.take(token, OTHER)
        image = self.broker.take(token, SESSION)
        self.assertEqual(image.mime, "image/png")
        self.assertEqual(base64.b64decode(image.data), PNG)
        self.assertEqual(
            image.content(),
            {
                "type": "image",
                "mimeType": "image/png",
                "data": base64.b64encode(PNG).decode(),
            },
        )
        with self.assertRaises(attachments.AttachmentError):
            self.broker.take(token, SESSION)

    def test_decline_and_non_boolean_consent_read_no_content_and_issue_no_grant(self):
        for answer in (False, None, "yes", 1):
            with (
                self.subTest(answer=answer),
                patch.object(sharing.Export, "read") as read,
            ):
                with self.assertRaises(attachments.AttachmentError):
                    self.offer(consent=lambda _: answer)
                read.assert_not_called()
                self.assertEqual(self.broker.grants, {})
                self.assertEqual(self.broker.offering, 0)

    def test_interruption_closes_descriptor_and_releases_capacity(self):
        descriptors = []

        def consent(_):
            # The actual selected descriptor must be closed even for BaseException.
            descriptors.extend(
                int(p.name)
                for p in Path("/proc/self/fd").iterdir()
                if p.exists() and os.path.samefile(p, self.path)
            )
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.offer(consent=consent)
        self.assertTrue(descriptors)
        for fd in descriptors:
            with self.assertRaises(OSError):
                os.fstat(fd)
        self.assertEqual(self.broker.offering, 0)
        self.assertEqual(self.broker.grants, {})
        self.assertTrue(self.offer())

    def test_expiry_revocation_close_and_unknown_tokens_do_not_regrant(self):
        token = self.offer()
        self.now += attachments.GRANT_LIFETIME
        for invalid in (token, "unknown", str(self.path), [], None):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(attachments.AttachmentError),
            ):
                self.broker.take(invalid, SESSION)
        token = self.offer()
        self.broker.revoke(token)
        with self.assertRaises(attachments.AttachmentError):
            self.broker.take(token, SESSION)
        token = self.offer()
        self.broker.close()
        with self.assertRaises(attachments.AttachmentError):
            self.broker.take(token, SESSION)
        with patch.object(sharing, "Export") as export:
            with self.assertRaises(attachments.AttachmentError):
                self.offer()
            export.assert_not_called()

    def test_invalid_session_refuses_before_filesystem_or_consent(self):
        consent = Mock()
        with patch.object(sharing, "Export") as export:
            for session in (None, {}, "", "other", "../ses_escape", "ses_" + "x" * 130):
                with (
                    self.subTest(session=session),
                    self.assertRaises(attachments.AttachmentError),
                ):
                    self.broker.offer(self.path, session, consent=consent)
            export.assert_not_called()
        consent.assert_not_called()

    def test_full_broker_refuses_before_reading_and_expired_grants_release_capacity(
        self,
    ):
        for _ in range(attachments.MAX_PENDING):
            self.offer()
        with patch.object(sharing, "Export") as export:
            with self.assertRaises(attachments.AttachmentError):
                self.offer()
            export.assert_not_called()
        self.now += attachments.GRANT_LIFETIME
        self.assertTrue(self.offer())
        self.assertEqual(len(self.broker.grants), 1)

    def test_concurrent_host_prompts_reserve_capacity_and_close_cancels_pending_offer(
        self,
    ):
        def nested_consent(_):
            with patch.object(attachments, "MAX_PENDING", 1):
                with self.assertRaises(attachments.AttachmentError):
                    self.offer()
            self.broker.close()
            return True

        with self.assertRaises(attachments.AttachmentError):
            self.offer(consent=nested_consent)
        self.assertEqual(self.broker.offering, 0)
        self.assertEqual(self.broker.grants, {})

    def test_file_replacement_during_consent_does_not_authorize_replacement_bytes(self):
        def replace(path):
            path.unlink()
            path.write_bytes(PNG + b"replacement")
            return True

        with self.assertRaises(attachments.AttachmentError):
            self.offer(consent=replace)
        self.assertEqual(self.broker.grants, {})

    def test_directory_link_special_protected_and_oversize_selections_refuse(self):
        protected = self.root / "protected"
        protected.mkdir()
        private = protected / "image.png"
        private.write_bytes(PNG)
        symlink = self.root / "symlink"
        symlink.symlink_to(self.path)
        hardlink = self.root / "hardlink"
        os.link(private, hardlink)
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        large = self.root / "large"
        with large.open("wb") as stream:
            stream.truncate(sharing.MAX_IMAGE + 1)
        self.broker.protected = (protected,)
        for source in (self.root, private, symlink, hardlink, fifo, large):
            with (
                self.subTest(source=source),
                self.assertRaises(attachments.AttachmentError),
            ):
                self.broker.offer(source, SESSION, consent=lambda _: True)
        self.assertEqual(self.broker.grants, {})
        self.assertEqual(self.broker.offering, 0)

    def test_source_errors_and_representations_do_not_disclose_path_or_content(self):
        with patch.object(sharing.Export, "read", side_effect=OSError("private-error")):
            with self.assertRaises(attachments.AttachmentError) as raised:
                self.offer()
        for secret in ("private-error", str(self.path)):
            self.assertNotIn(secret, str(raised.exception))
        token = self.offer()
        grant = self.broker.grants[token]
        self.assertNotIn(base64.b64encode(PNG).decode(), repr(grant))
        image = self.broker.take(token, SESSION)
        self.assertNotIn(image.data, repr(image))

    def test_offer_reads_only_after_host_consent_and_no_staging_files_are_created(self):
        order = []
        original = sharing.Export.read

        def consent(_):
            order.append("consent")
            return True

        def read(export, *args, **kwargs):
            order.append("read")
            return original(export, *args, **kwargs)

        before = sorted(self.root.rglob("*"))
        with patch.object(sharing.Export, "read", read):
            self.offer(consent=consent)
        self.assertEqual(order, ["consent", "read"])
        self.assertEqual(sorted(self.root.rglob("*")), before)


class AttachmentMCPTests(unittest.TestCase):
    INITIALIZE = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "clientInfo": {"name": "opencode", "version": "2.4.1"},
        },
    }

    def setUp(self):
        AttachmentBrokerTests.setUp(self)
        self.consent = Mock(return_value=True)
        self.check = Mock()
        self.protocol = self.protocol_instance()
        self.protocol.reply(self.INITIALIZE)
        self.protocol.reply({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.protocol.reply({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.protocol.activate()

    def protocol_instance(self):
        return attachments.MCP(
            self.broker,
            SESSION,
            version="2.4.1",
            consent=self.consent,
            check=self.check,
        )

    def call(self, name, arguments, *, session=SESSION, meta=None):
        return self.protocol.reply(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": name,
                    "arguments": arguments,
                    "_meta": {"ai.opencode/sessionID": session}
                    if meta is None
                    else meta,
                },
            }
        )["result"]

    def test_native_session_metadata_is_required_before_any_host_proposal(self):
        for meta in ({}, {"ai.opencode/sessionID": OTHER}, {"sessionID": SESSION}, []):
            with self.subTest(meta=meta), patch.object(sharing, "Export") as export:
                result = self.call("request_image", {"path": str(self.path)}, meta=meta)
                self.assertTrue(result["isError"])
                self.assertIn(
                    "belongs to another session"
                    if meta == {"ai.opencode/sessionID": OTHER}
                    else "did not provide a verified caller session",
                    result["content"][0]["text"],
                )
                export.assert_not_called()
        self.consent.assert_not_called()
        self.check.assert_not_called()

    def test_request_only_returns_capability_and_read_consumes_captured_bytes_once(
        self,
    ):
        offered = self.call("request_image", {"path": str(self.path)})
        self.assertNotIn("isError", offered)
        self.consent.assert_called_once_with(self.path)
        self.assertNotIn(base64.b64encode(PNG).decode(), json.dumps(offered))
        token = json.loads(offered["content"][0]["text"])["id"]
        self.path.unlink()
        wrong = self.call("read_attachment", {"id": token}, session=OTHER)
        self.assertTrue(wrong["isError"])
        image = self.call("read_attachment", {"id": token})["content"][0]
        self.assertEqual(image["type"], "image")
        self.assertEqual(base64.b64decode(image["data"]), PNG)
        self.assertTrue(self.call("read_attachment", {"id": token})["isError"])

    def test_remote_path_proposal_is_not_host_authorization(self):
        self.consent.return_value = False
        with patch.object(sharing.Export, "read") as read:
            result = self.call("request_image", {"path": str(self.path)})
        self.assertTrue(result["isError"])
        read.assert_not_called()
        self.assertEqual(self.broker.grants, {})

    def test_denied_proposals_cannot_distinguish_missing_file_directory_or_regular_file(
        self,
    ):
        self.consent.return_value = False
        protected = self.root / "protected.png"
        protected.write_bytes(PNG)
        self.broker.protected = (protected,)
        responses = []
        with patch.object(sharing.Export, "read") as read:
            for path in (self.path, self.root, self.root / "missing", protected):
                responses.append(self.call("request_image", {"path": str(path)}))
            with patch.object(
                sharing, "Export", side_effect=PermissionError("private")
            ):
                responses.append(self.call("request_image", {"path": str(self.path)}))
            read.assert_not_called()
        self.assertEqual(self.consent.call_count, 5)
        self.assertEqual(responses, [responses[0]] * 5)
        self.assertEqual(
            responses[0],
            {
                "isError": True,
                "content": [
                    {
                        "type": "text",
                        "text": "Host image attachment was not authorized.",
                    }
                ],
            },
        )

    def test_capability_cannot_be_substituted_with_path_or_granted_by_extra_arguments(
        self,
    ):
        cases = (
            ("read_attachment", {"id": str(self.path)}),
            ("request_image", {"path": str(self.path), "approved": True}),
            ("request_image", {"path": str(self.path), "sessionID": SESSION}),
            ("request_image", {"path": "relative.png"}),
            ("request_image", {"path": "\0"}),
            ("request_image", {"path": "\ud800"}),
            ("request_image", {"path": "x" * 4097}),
            ("request_image", []),
            ("read_file", {"path": str(self.path)}),
            ([], {}),
        )
        for name, arguments in cases:
            with self.subTest(name=name, arguments=repr(arguments)):
                self.assertTrue(self.call(name, arguments)["isError"])
        self.consent.assert_not_called()

    def test_metadata_state_rechecked_after_approval_and_before_redemption(self):
        self.check.side_effect = [None, attachments.AttachmentError("Target changed.")]
        with patch.object(sharing.Export, "read") as read:
            self.assertTrue(
                self.call("request_image", {"path": str(self.path)})["isError"]
            )
        read.assert_not_called()
        self.assertEqual(self.broker.grants, {})

    def test_protocol_catalog_initialization_bounds_and_private_error_sanitization(
        self,
    ):
        catalog = self.protocol.reply(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
            }
        )["result"]["tools"]
        self.assertEqual(
            {tool["name"] for tool in catalog}, {"request_image", "read_attachment"}
        )
        for tool in catalog:
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
        for request in (
            None,
            [],
            {},
            {"jsonrpc": "1.0", "method": "ping"},
            {"jsonrpc": "2.0", "id": False, "method": "ping"},
        ):
            with self.subTest(request=request), self.assertRaises(sharing.ShareError):
                self.protocol.reply(request)
        self.assertIsNone(
            self.protocol.reply(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
        )
        self.protocol.negotiated = False
        result = self.protocol.reply(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        self.assertEqual(result["error"]["code"], -32000)
        self.protocol.negotiated = True
        self.consent.side_effect = OSError("private-error-sentinel")
        result = self.call("request_image", {"path": str(self.path)})
        self.assertTrue(result["isError"])
        self.assertNotIn("private-error-sentinel", json.dumps(result))

    def test_requires_matching_native_handshake_before_tool_authority(self):
        self.protocol = self.protocol_instance()
        bad_identity = {
            **self.INITIALIZE,
            "params": {
                **self.INITIALIZE["params"],
                "clientInfo": {"name": "opencode", "version": "2.4.0"},
            },
        }
        with self.assertRaisesRegex(attachments.AttachmentError, "handshake"):
            self.protocol.reply(bad_identity)
        self.protocol.reply(self.INITIALIZE)
        catalog = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        self.assertIn("error", self.protocol.reply(catalog))
        with self.assertRaisesRegex(attachments.AttachmentError, "handshake"):
            self.protocol.activate()
        self.protocol.reply({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertIn("tools", self.protocol.reply(catalog)["result"])
        self.assertTrue(self.call("request_image", {"path": str(self.path)})["isError"])
        self.check.assert_not_called()
        self.consent.assert_not_called()
        self.assertFalse(self.broker.grants)

    def test_coalesced_large_one_use_redemptions_are_backpressured_not_lost(self):
        self.protocol = self.protocol_instance()
        content = PNG + b"x" * (6 * 1024 * 1024 - len(PNG))
        self.path.write_bytes(content)
        first = self.broker.offer(self.path, SESSION, consent=lambda _: True)
        second = self.broker.offer(self.path, SESSION, consent=lambda _: True)
        program = r"""
import base64, hashlib, json, os, sys
first, second, session, expected = sys.argv[1:]
def request(ident, method, params={}):
    return {'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params}
def send(value):
    os.write(1, json.dumps(value).encode() + b'\n')
def reply():
    return json.loads(sys.stdin.buffer.readline(12 * 1024 * 1024 + 1))
send(request(1, 'initialize', {
    'protocolVersion': '2025-11-25',
    'clientInfo': {'name': 'opencode', 'version': '2.4.1'}
}))
assert reply()['id'] == 1
send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
send(request(2, 'tools/list'))
assert reply()['id'] == 2
batch = b''.join(json.dumps(request(ident, 'tools/call', {
    'name': 'read_attachment', 'arguments': {'id': token},
    '_meta': {'ai.opencode/sessionID': session}
})).encode() + b'\n' for ident, token in ((3, first), (4, second)))
assert len(batch) < 4096
os.write(1, batch)
for ident in (3, 4):
    value = reply()
    assert value['id'] == ident and not value['result'].get('isError')
    image = value['result']['content'][0]
    assert image['type'] == 'image'
    assert hashlib.sha256(base64.b64decode(image['data'], validate=True)).hexdigest() == expected
sys.stderr.write('DELIVERED_BOTH\n')
"""
        child = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                program,
                first,
                second,
                SESSION,
                hashlib.sha256(content).hexdigest(),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            sharing.serve(
                child, None, protocol=self.protocol, ready=self.protocol.activate
            )
            self.assertEqual(child.wait(timeout=5), 0)
            self.assertEqual(child.stderr.read(), b"DELIVERED_BOTH\n")
            self.assertEqual(self.broker.grants, {})
        finally:
            child.stdin.close()
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
            child.stdout.close()
            child.stderr.close()


class AttachmentCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.image = self.root / "image.png"
        self.image.write_bytes(PNG)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}))
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.stdout))
        self.stack.enter_context(contextlib.redirect_stderr(self.stderr))
        self.data = {
            "id": "synthetic-workspace",
            "opencode_version": "2.3.0",
            "project": "/synthetic/project",
            "container_project": "/workspace/project",
            "container": "synthetic-container",
            "home": "/home/synthetic",
        }
        self.connection = {"url": "http://127.0.0.1:12345", "password": "x" * 32}
        self.facade = SimpleNamespace(
            BoxError=box.BoxError,
            PACKAGE=box.PACKAGE,
            APP=self.root / "app",
            CONTAINER_OPENCODE=box.CONTAINER_OPENCODE,
            ContainerCommand=box.ContainerCommand,
            state_root=lambda: self.root / "state",
            vault=SimpleNamespace(vault_path=lambda: self.root / "vault/vault.age"),
            valid_session_id=box.valid_session_id,
            container_project=box.container_project,
            maintenance_host_env=box.maintenance_host_env,
            find_workspace=Mock(return_value=(self.root / "workspace.json", self.data)),
            check_backend=Mock(),
            run=Mock(),
        )
        self.facade.workspace_service = SimpleNamespace(
            attach=Mock(side_effect=lambda *_: contextlib.nullcontext(self.connection)),
            client_command=Mock(wraps=workspace_service.client_command),
        )
        self.forbidden = {}
        for name in (
            "setup",
            "start_container",
            "launch_credentials",
            "refresh_sessions",
            "sync_config",
        ):
            self.forbidden[name] = Mock(side_effect=AssertionError(name))
            setattr(self.facade, name, self.forbidden[name])
        self.requests = []

        def native(_, data, connection, session, *, prompt=None, broker=False):
            self.assertIs(data, self.data)
            self.assertIs(connection, self.connection)
            self.assertEqual(session, SESSION)
            self.requests.append(prompt)
            return {"ok": True, **({"message": prompt["id"]} if prompt else {})}

        self.native = self.stack.enter_context(
            patch.object(attachments, "native", side_effect=native)
        )

    def tearDown(self):
        for method in self.forbidden.values():
            method.assert_not_called()

    def cli(self, *args):
        return attachments.cli(self.facade, [*args, "--session", SESSION])

    def test_direct_explicit_file_uses_private_inline_snapshot_not_host_file_uri(self):
        self.assertEqual(self.cli(str(self.image), "--acknowledge-risk"), 0)
        self.assertEqual(len(self.requests), 2)
        prompt = self.requests[1]
        self.assertEqual(prompt["delivery"], "queue")
        self.assertIs(prompt["resume"], False)
        file = prompt["files"][0]
        self.assertEqual(file["source"], {"type": "inline"})
        self.assertEqual(
            file["uri"], "data:image/png;base64," + base64.b64encode(PNG).decode()
        )
        self.assertEqual(file["name"], "host-image.png")
        self.assertNotIn(str(self.image), json.dumps(prompt))
        self.assertIn(prompt["id"], self.stdout.getvalue())
        self.assertNotIn(base64.b64encode(PNG).decode(), self.stderr.getvalue())
        self.assertIn("SECURITY WARNING", self.stderr.getvalue())

    def test_send_is_explicit_and_custom_message_is_not_printed(self):
        self.cli(
            str(self.image),
            "--acknowledge-risk",
            "--send",
            "--text",
            "synthetic private prompt",
        )
        self.assertTrue(self.requests[1]["resume"])
        self.assertEqual(self.requests[1]["text"], "synthetic private prompt")
        self.assertNotIn(
            "synthetic private prompt", self.stdout.getvalue() + self.stderr.getvalue()
        )

    def test_container_refuses_before_workspace_lookup(self):
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}),
            self.assertRaises(box.BoxError),
        ):
            self.cli(str(self.image), "--acknowledge-risk")
        self.facade.find_workspace.assert_not_called()
        self.facade.check_backend.assert_not_called()

    def test_broker_cannot_receive_blanket_approval_or_direct_send_options(self):
        for args in (
            ("--broker", "--acknowledge-risk"),
            ("--broker", "--send"),
            ("--broker", str(self.image)),
            ("--broker", "--text", "anything"),
            (),
        ):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                self.cli(*args)
        self.facade.find_workspace.assert_not_called()

    def test_unsupported_workspaces_refuse_without_backend_or_owner_start(self):
        for data in (
            None,
            {},
            {"opencode_version": "1.19.5"},
            {"opencode_version": "2.99.0"},
            {"opencode_version": "2.4.1-rc.1"},
            {"opencode_version": "2.04.1"},
            {"opencode_version": {}},
        ):
            self.facade.find_workspace.return_value = (
                self.root / "workspace.json",
                data,
            )
            with self.subTest(data=data), self.assertRaises(box.BoxError):
                self.cli(str(self.image), "--acknowledge-risk")
        self.facade.check_backend.assert_not_called()
        self.facade.workspace_service.attach.assert_not_called()

    def test_future_stable_release_uses_broker_but_never_direct_preflight(self):
        self.data["opencode_version"] = "2.99.0"
        with self.assertRaisesRegex(box.BoxError, "Use 'oc-box attach --broker'"):
            self.cli(str(self.image), "--acknowledge-risk")
        self.facade.workspace_service.attach.assert_not_called()
        self.native.assert_not_called()
        with (
            patch.object(attachments.sys.stdin, "isatty", return_value=True),
            patch.object(attachments.sys.stderr, "isatty", return_value=True),
            patch.object(attachments, "serve") as serve,
        ):
            self.assertEqual(self.cli("--broker"), 0)
        self.native.assert_called_once_with(
            self.facade, self.data, self.connection, SESSION, broker=True
        )
        serve.assert_called_once()

    def test_lease_failure_is_distinct_from_unverified_admission(self):
        self.facade.workspace_service.attach.side_effect = box.BoxError(
            "raw-private-owner-error"
        )
        with self.assertRaisesRegex(
            box.BoxError, "^No verified running private workspace owner"
        ):
            self.cli(str(self.image), "--acknowledge-risk")
        self.native.assert_not_called()

    def test_broker_requires_host_tty_and_pins_explicit_session_with_one_lease(self):
        with (
            patch.object(attachments.sys.stdin, "isatty", return_value=False),
            self.assertRaises(box.BoxError),
        ):
            self.cli("--broker")
        self.facade.find_workspace.assert_not_called()
        with (
            patch.object(attachments.sys.stdin, "isatty", return_value=True),
            patch.object(attachments.sys.stderr, "isatty", return_value=True),
            patch.object(attachments, "serve") as serve,
        ):
            self.assertEqual(self.cli("--broker", "--project", "/synthetic/project"), 0)
        self.facade.find_workspace.assert_called_once_with(
            Path("/synthetic/project"), None
        )
        self.facade.workspace_service.attach.assert_called_once()
        self.assertEqual(serve.call_args.args[3], SESSION)
        self.assertTrue(serve.call_args.args[4].closed)

    def test_refused_target_is_checked_before_host_image_read_or_consent(self):
        self.native.side_effect = attachments.AttachmentError(
            "Target has a staged revert."
        )
        with patch.object(sharing, "Export") as opened, self.assertRaises(box.BoxError):
            self.cli(str(self.image), "--acknowledge-risk")
        opened.assert_not_called()

    def test_unverified_send_never_retries_or_claims_admission(self):
        self.native.side_effect = [
            {"ok": True},
            attachments.AttachmentError("Image admission was not verified."),
        ]
        with self.assertRaises(box.BoxError):
            self.cli(str(self.image), "--acknowledge-risk")
        self.assertEqual(self.native.call_count, 2)
        self.assertNotIn("input admitted", self.stdout.getvalue())

    def test_native_adapter_passes_bytes_only_on_stdin_and_sanitizes_errors(self):
        prompt = {
            "id": "msg_fixture",
            "text": "synthetic-private-message",
            "files": [
                {"uri": "data:image/png;base64," + base64.b64encode(PNG).decode()}
            ],
        }
        self.facade.run.return_value = subprocess.CompletedProcess(
            [], 0, json.dumps({"ok": True, "message": "msg_fixture"}), ""
        )
        response = REAL_NATIVE(
            self.facade, self.data, self.connection, SESSION, prompt=prompt
        )
        self.assertEqual(response, {"ok": True, "message": "msg_fixture"})
        command = self.facade.run.call_args.args[0]
        options = self.facade.run.call_args.kwargs
        self.assertEqual(json.loads(options["input"])["prompt"], prompt)
        self.assertTrue(options["private"])
        self.assertFalse(options["check"])
        self.assertIs(options["env"], command.environment)
        self.assertIn("OPENCODE_PASSWORD", command.environment)
        for value in (
            self.connection["password"],
            base64.b64encode(PNG).decode(),
            prompt["text"],
        ):
            self.assertNotIn(value, json.dumps(list(command)))
        self.assertNotIn("--data", command)
        for output in (
            "private-native-error",
            json.dumps({"ok": False, "category": "private-path-sentinel"}),
            json.dumps({"ok": True, "message": "msg_wrong"}),
        ):
            self.facade.run.return_value = subprocess.CompletedProcess(
                [], 1, output, ""
            )
            with (
                self.subTest(output=output),
                self.assertRaises(attachments.AttachmentError) as raised,
            ):
                REAL_NATIVE(
                    self.facade, self.data, self.connection, SESSION, prompt=prompt
                )
            self.assertNotIn("private-", str(raised.exception))

    def test_broker_adapter_only_performs_read_only_target_preflight(self):
        self.facade.run.return_value = subprocess.CompletedProcess(
            [], 0, json.dumps({"ok": True}), ""
        )
        self.assertEqual(
            REAL_NATIVE(self.facade, self.data, self.connection, SESSION, broker=True),
            {"ok": True},
        )
        value = json.loads(self.facade.run.call_args.kwargs["input"])
        self.assertEqual(value["operation"], "broker-check")
        self.assertNotIn("prompt", value)

    def test_broker_relay_eof_refuses_before_or_after_readiness(self):
        for negotiated in (False, True):
            with self.subTest(negotiated=negotiated), attachments.Broker() as broker:
                relay = Mock()
                relay.poll.return_value = None

                def pump(process, export, *, ready, protocol):
                    if negotiated:
                        protocol.reply(AttachmentMCPTests.INITIALIZE)
                        protocol.reply(
                            {"jsonrpc": "2.0", "method": "notifications/initialized"}
                        )
                        protocol.reply(
                            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
                        )
                        ready()
                    # Unexpected EOF can look like a clean child exit.
                    return None

                self.data["opencode_version"] = "2.4.1"
                with (
                    patch.object(attachments.subprocess, "Popen", return_value=relay),
                    patch.object(sharing, "serve", side_effect=pump),
                    self.assertRaises(attachments.AttachmentError) as raised,
                ):
                    attachments.serve(
                        self.facade, self.data, self.connection, SESSION, broker
                    )
                self.assertEqual(
                    str(raised.exception),
                    attachments.BROKER_DISCONNECTED
                    if negotiated
                    else attachments.BROKER_UNVERIFIED,
                )
                self.assertEqual(
                    "handshake verified:" in self.stdout.getvalue(), negotiated
                )
                self.assertTrue(broker.closed)
        self.native.assert_not_called()


class HostImageApprovalTests(unittest.TestCase):
    def test_real_host_tty_requires_explicit_approval_and_escapes_selected_name(self):
        script = """
import fcntl, json, os, termios
from pathlib import Path
from oc_box import attachments
os.setsid()
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
result = attachments.approve(Path('/synthetic/Screenshot\\\\nname.png'), 'ses_fixture')
print('APPROVAL_RESULT=' + json.dumps(result), flush=True)
"""
        for answer, expected in ((b"attach\n", True), (b"no\n", False)):
            with self.subTest(answer=answer):
                master, slave = pty.openpty()
                child = subprocess.Popen(
                    [sys.executable, "-B", "-c", script],
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    env={
                        "PATH": os.environ["PATH"],
                        "PYTHONPATH": str(box.PACKAGE.parent),
                    },
                )
                os.close(slave)
                output = bytearray()
                deadline = time.monotonic() + 5

                def until(marker):
                    while marker not in output and time.monotonic() < deadline:
                        ready, _, _ = select.select(
                            [master], [], [], max(0, deadline - time.monotonic())
                        )
                        if ready:
                            try:
                                chunk = os.read(master, 65536)
                            except OSError:
                                break
                            if not chunk:
                                break
                            output.extend(chunk)
                    self.assertIn(marker, output)

                try:
                    until(b"to disclose this image:")
                    self.assertNotIn(b"APPROVAL_RESULT=", output)
                    self.assertIsNone(child.poll())
                    os.write(master, answer)
                    until(b"APPROVAL_RESULT=" + (b"true" if expected else b"false"))
                    self.assertEqual(child.wait(timeout=3), 0)
                    self.assertIn(b"SECURITY WARNING", output)
                    self.assertIn(b"Destination session: ses_fixture", output)
                finally:
                    if child.poll() is None:
                        child.terminate()
                        child.wait(timeout=3)
                    os.close(master)

    def test_non_tty_and_timeout_cannot_approve_without_explicit_direct_acknowledgement(
        self,
    ):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            with patch.object(attachments.sys.stdin, "isatty", return_value=False):
                with self.assertRaises(attachments.AttachmentError):
                    attachments.approve(Path("/synthetic/image.png"), SESSION)
                self.assertTrue(
                    attachments.approve(
                        Path("/synthetic/image.png"), SESSION, acknowledged=True
                    )
                )
            with (
                patch.object(attachments.sys.stdin, "isatty", return_value=True),
                patch.object(attachments.sys.stderr, "isatty", return_value=True),
                patch.object(attachments.select, "select", return_value=([], [], [])),
            ):
                self.assertFalse(
                    attachments.approve(Path("/synthetic/image.png"), SESSION)
                )
        self.assertIn("request expired", output.getvalue())
