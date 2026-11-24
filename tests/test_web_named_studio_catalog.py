"""Synthetic named Studio catalog with one tracked private native socket."""

from dataclasses import replace
import json
import os
import socket
from threading import Thread
from time import monotonic
import unittest
from unittest.mock import Mock, patch

import test_web_pake_flow as pake

from oc_box import (
    oc_box as box,
    web_app_ingress,
    web_local_origins,
    web_named_headers,
    web_named_owner,
    web_named_relay,
    web_named_studio_catalog as catalog,
    web_named_transport,
    web_native_pair,
    web_studio_send,
)


_PROJECT = "/synthetic/repo/work & branch"
_ROOT = "/synthetic/repo"
_PASSWORD = "synthetic_unused_control_" + "q" * 24
_COOKIE = "K" * 10 + "." + "s" * 43


def admitted_app(owner, origins, cookie):
    """Trusted grant fixture; intentionally not a new sign-in implementation."""
    binding = owner.binding
    flow = pake.pake.PakeFlow(
        binding,
        store=pake.accounts.AccountStore(
            pake.SETUP,
            (pake.accounts.Account("fixture", (binding.workspace_id,), pake.REAL),),
        ),
        backend=pake.SyntheticBackend(),
        sizes=pake.SIZES,
        auth_origin=origins.account.value,
    )
    app = owner.compose(flow=flow, claim_bootstrap=lambda _: None)
    token = app.sign_in.admission.issue(
        binding, principal="fixture", native_cookie=cookie
    )
    return app, token


def request(origins, token, **overrides):
    headers = {
        "Host": origins.workspace.value.removeprefix("https://"),
        "Cookie": f"{web_app_ingress.APP_COOKIE_NAME}={token}",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "same-origin",
        "Sec-Fetch-Dest": "empty",
    }
    headers.update(overrides)
    return web_named_transport.NamedRequest(
        "workspace",
        "GET",
        catalog.PATH,
        tuple((name, value) for name, value in headers.items() if value is not None),
        b"",
    )


def row(ident="ses_current", **fields):
    return {
        "id": ident,
        "title": "Synthetic <title>\u202e",
        "location": {"directory": _PROJECT},
        "projectID": "synthetic_id",
        "subpath": "work & branch",
        "time": {"created": 1, "updated": 7},
        "metadata": {"secret": "OMITTED"},
        **fields,
    }


def page(rows, *, following=None):
    return {"data": rows, "cursor": {"previous": None, "next": following}}


class FixtureNative:
    def __init__(self, identity):
        self.info = {
            "version": identity.version,
            "pid": 42,
            "hostname": "synthetic",
            "urls": [identity.endpoint],
            "paths": {"tmp": "/synthetic/tmp"},
            "capabilities": {"persistentPty": True},
        }
        self.location = {
            "directory": _PROJECT,
            "project": {
                "id": "synthetic_id",
                "directory": _ROOT,
                "canonical": _ROOT,
            },
        }
        self.page = page(
            [
                row(),
                row(
                    "ses_child",
                    parentID="ses_parent",
                    time={"created": 2, "updated": 8, "archived": 9},
                ),
            ],
            following="native_continuation",
        )
        self.requests = []
        self.second_info = self.second_location = None
        self.hook = lambda _: None

    def serve(self, peer):
        with peer.makefile("rb") as stream:
            while line := stream.readline(4096):
                method, target, version = line.decode("ascii").strip().split(" ")
                assert (method, version) == ("GET", "HTTP/1.1")
                headers = {}
                while (line := stream.readline(4096)) != b"\r\n":
                    if not line:
                        return
                    key, value = line.decode("ascii").strip().split(": ", 1)
                    assert key not in headers
                    headers[key] = value
                already = target in (path for path, _ in self.requests)
                self.requests.append((target, headers))
                self.hook(target)
                if target == catalog._INFO:
                    value = (
                        self.second_info
                        if already and self.second_info is not None
                        else self.info
                    )
                elif target == catalog._LOCATION:
                    value = (
                        self.second_location
                        if already and self.second_location is not None
                        else self.location
                    )
                elif target == catalog._list_path(_PROJECT):
                    value = self.page
                else:
                    raise AssertionError("Unexpected catalog route")
                body = json.dumps(value, separators=(",", ":")).encode("ascii")
                closing = headers["Connection"]
                wire = (
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    f"Connection: {closing}\r\nContent-Length: {len(body)}\r\n\r\n"
                ).encode("ascii")
                peer.sendall(wire + body)
                if closing == "close":
                    peer.shutdown(socket.SHUT_WR)
                    return


class NamedStudioCatalogTests(unittest.TestCase):
    def setUp(self):
        self.apply_patch(patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}))
        self.identity = web_named_owner.OwnerIdentity(
            "a" * 32, "b" * 32, "c" * 64, "2.4.1", "http://127.0.0.1:49153"
        )
        self.observe = Mock(return_value=self.identity)
        self.origins = web_local_origins.LocalOrigins()
        self.origin = self.origins.workspace.value
        self.authority = self.origin.removeprefix("https://")
        self.native = FixtureNative(self.identity)
        self.children = {}
        self.errors = []
        self.confirmed = True
        self.starts = 0
        control, controller = socket.socketpair()
        self.addCleanup(control.close)
        self.addCleanup(controller.close)
        self.addCleanup(self.assertEqual, self.errors, [])
        self.lease = web_named_owner.VerifiedOwnerLease(
            self.identity,
            control,
            _PROJECT,
            "/synthetic/home",
            _PASSWORD,
            self.observe,
            self.connect,
        )
        self.owner = web_named_owner.NamedOwnerBinding(
            box, origins=self.origins, lease=self.lease
        )
        self.addCleanup(self.owner.close)
        self.binding = self.owner.binding
        cookie = web_native_pair.NativeCookie(
            self.binding,
            self.identity.endpoint,
            self.authority,
            _COOKIE,
            version=self.identity.version,
            owner_current=self.owner._owner_current,
        )
        app, self.token = admitted_app(self.owner, self.origins, cookie)
        self.admission = app.sign_in.admission
        self.browser = self.admission.lookup(self.binding, self.token)
        self.handler = catalog.NamedStudioCatalog(self.owner)
        self.apply_patch(
            patch.object(
                web_named_relay._WorkspaceChild,
                "finish",
                lambda child, deadline, check: self.finish(child, deadline, check),
            )
        )
        self.apply_patch(
            patch.object(
                web_named_relay._WorkspaceChild, "close", lambda child: self.reap(child)
            )
        )

    def apply_patch(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()

    def connect(self, identity, *, track, check, deadline):
        self.assertIs(identity, self.identity)
        self.assertTrue(callable(track.start_workspace_relay))
        self.assertEqual(len(self.admission._uses), 1)
        self.assertGreater(deadline, monotonic())
        check()
        self.starts += 1
        child = web_named_relay._WorkspaceChild()
        host, peer = socket.socketpair()
        peer.settimeout(2)
        track(host)
        track._io.register_workspace(child)
        child._attempted = True

        def serve():
            with peer:
                try:
                    self.native.serve(peer)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as error:
                    self.errors.append(error)

        runner = self.children[child] = Thread(target=serve)
        runner.start()
        check()

    def reap(self, child):
        runner = self.children.get(child)
        if runner is not None:
            runner.join(timeout=2)
            self.assertFalse(runner.is_alive())
            if self.confirmed:
                child._exit = 0

    def finish(self, child, deadline, check):
        self.assertGreater(deadline, monotonic())
        self.reap(child)
        check()
        if not child.confirmed:
            raise OSError("PRIVATE SYNTHETIC CLEANUP DIAGNOSTIC")

    def fetch(self, req=None, *, check=lambda: None, deadline=None):
        if req is None:
            req = request(self.origins, self.token)
        server, client = socket.socketpair()
        with server, client:
            self.handler.respond(
                server,
                req,
                monotonic() + 2 if deadline is None else deadline,
                check=check,
            )
            server.shutdown(socket.SHUT_WR)  # Only the outer worker retires the stream.
            client.settimeout(1)
            with client.makefile("rb") as stream:
                head, _, body = stream.read(16384).partition(b"\r\n\r\n")
            return head, json.loads(body)

    def refused(self, *args, **kwargs):
        with self.assertRaisesRegex(
            web_named_headers.NamedRequestError,
            r"^Named web request was not admitted\.$",
        ):
            self.fetch(*args, **kwargs)

    def test_exact_project_one_page_and_tight_host_owned_projection(self):
        raw_write = web_named_transport._write_response

        def write(stream, reply, deadline, *, check):
            check()
            self.assertEqual(len(self.admission._uses), 1)
            self.assertTrue(all(child.confirmed for child in self.children))
            raw_write(stream, reply, deadline, check=check)

        with patch.object(web_named_transport, "_write_response", side_effect=write):
            head, value = self.fetch(
                request(self.origins, self.token, **{"X-Synthetic": "never forwarded"})
            )
        self.assertEqual(self.starts, 1)
        self.assertIn(b"HTTP/1.1 200 OK\r\n", head)
        self.assertNotIn(b"Set-Cookie", head)
        self.assertEqual(
            value,
            {
                "sessions": [
                    {
                        "id": "ses_current",
                        "title": "Synthetic <title>",
                        "archived": False,
                        "child": False,
                    },
                    {
                        "id": "ses_child",
                        "title": "Synthetic <title>",
                        "archived": True,
                        "child": True,
                    },
                ],
                "hasEarlier": True,
            },
        )
        expected_page = f"/api/session?directory=%2Fsynthetic%2Frepo%2Fwork%20%26%20branch&order=desc&limit={catalog.RECENT_LIMIT}"
        self.assertEqual(
            [target for target, _ in self.native.requests],
            ["/api/info", "/api/location", expected_page, "/api/info", "/api/location"],
        )
        for index, (_, fields) in enumerate(self.native.requests):
            self.assertEqual(
                fields,
                {
                    "Host": self.authority,
                    "Origin": self.origin,
                    "Cookie": web_native_pair._cookie_name(self.authority)
                    + "="
                    + _COOKIE,
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "Connection": "close" if index == 4 else "keep-alive",
                },
            )
        self.assertTrue(
            self.admission.begin_revocation(self.binding, self.browser).wait_drained(0)
        )

    def test_empty_short_and_full_windows_and_worst_case_output_bound(self):
        self.native.page = page([])
        self.assertEqual(self.fetch()[1], {"sessions": [], "hasEarlier": False})
        self.native = FixtureNative(self.identity)
        self.native.page = page([row(title="ñ" * (catalog.MAX_TITLE_BYTES + 1))])
        one = self.fetch()[1]
        self.assertFalse(one["hasEarlier"])
        self.assertEqual(
            len(one["sessions"][0]["title"].encode()), catalog.MAX_TITLE_BYTES
        )
        self.native = FixtureNative(self.identity)
        self.native.page = page(
            [
                row("ses_" + "a" * 122 + f"{i:02}", title='"' * catalog.MAX_TITLE_BYTES)
                for i in range(catalog.RECENT_LIMIT)
            ]
        )
        head, full = self.fetch()
        self.assertIn(b"200 OK", head)
        self.assertTrue(
            full["hasEarlier"]
        )  # Full page without cursor is still bounded/unknown.
        self.assertEqual(len(full["sessions"]), catalog.RECENT_LIMIT)

    def test_validated_app_grant_and_strict_browser_envelope_are_required(self):
        get = request(self.origins, self.token)
        for bad in (
            replace(get, role="account"),
            replace(get, method="POST"),
            replace(get, target=catalog.PATH + "?"),
            replace(get, target=catalog.PATH + "?directory=/elsewhere"),
            replace(get, target="/api/info"),
            replace(get, body=b"{}"),
            replace(get, headers=get.headers + (("host", self.authority),)),
            request(self.origins, "invalid"),
            request(self.origins, self.token, Cookie="opencode_session=" + _COOKIE),
            request(self.origins, self.token, Host="other.localhost"),
            request(self.origins, self.token, Origin="https://other.localhost"),
            request(self.origins, self.token, Referer="https://other.localhost/"),
            request(self.origins, self.token, **{"Sec-Fetch-Site": "same-site"}),
            request(self.origins, self.token, **{"Sec-Fetch-Site": None}),
            request(self.origins, self.token, **{"Sec-Fetch-Dest": "iframe"}),
            request(self.origins, self.token, **{"Content-Length": "01"}),
            request(self.origins, self.token, **{"Transfer-Encoding": "chunked"}),
            request(self.origins, self.token, **{"X-Forwarded-Host": self.authority}),
        ):
            self.refused(bad)
        self.refused(get, deadline=monotonic() - 1)
        self.assertTrue(self.admission.revoke(self.binding, self.browser))
        self.refused(get)
        self.assertEqual(self.starts, 0)

    def test_prechecks_refuse_before_list_and_postchecks_refuse_full_output(self):
        for changes in ("version", "location", "pid-after", "canonical-after"):
            with self.subTest(changes=changes):
                self.native = FixtureNative(self.identity)
                if changes == "version":
                    self.native.info["version"] = "2.4.2"
                elif changes == "location":
                    self.native.location["directory"] = _ROOT
                elif changes == "pid-after":
                    self.native.second_info = {**self.native.info, "pid": 7}
                else:
                    self.native.second_location = {
                        **self.native.location,
                        "project": {
                            **self.native.location["project"],
                            "canonical": "/elsewhere",
                        },
                    }
                with patch.object(web_named_transport, "_write_response") as write:
                    self.refused()
                write.assert_not_called()
                self.assertEqual(
                    sum(
                        target == catalog._list_path(_PROJECT)
                        for target, _ in self.native.requests
                    ),
                    int(changes.endswith("after")),
                )

    def test_bad_rows_cursors_and_wire_caps_withhold_entire_page(self):
        for label, bad in (
            ("foreign location", page([row(location={"directory": _ROOT})])),
            ("other project", page([row(projectID="different")])),
            ("subpath", page([row(subpath="different")])),
            ("bad title", page([row(title=None)])),
            ("parent type", page([row(parentID=False)])),
            ("self parent", page([row(parentID="ses_current")])),
            (
                "archive type",
                page([row(time={"created": 1, "updated": 2, "archived": True})]),
            ),
            ("duplicate ID", page([row(), row()])),
            ("cursor", page([row()], following="new&directory=/")),
            ("oversized reply", page([row(metadata="x" * web_studio_send._MAX_REPLY)])),
            (
                "oversized count",
                page([row(f"ses_{i}") for i in range(catalog.RECENT_LIMIT + 1)]),
            ),
            ("native cookie", page([row(title=_COOKIE)])),
            ("control credential", page([row(title=_PASSWORD)])),
            ("browser grant", page([row(title=self.token)])),
        ):
            with self.subTest(label=label):
                self.native = FixtureNative(self.identity)
                self.native.page = bad
                with patch.object(web_named_transport, "_write_response") as write:
                    self.refused()
                write.assert_not_called()
                self.assertEqual(
                    sum(
                        target == catalog._list_path(_PROJECT)
                        for target, _ in self.native.requests
                    ),
                    1,
                )

    def test_revoke_cancels_native_and_downstream_then_drains(self):
        revocations = []

        def hook(target):
            if target == catalog._list_path(_PROJECT):
                revocations.append(
                    self.admission.begin_revocation(self.binding, self.browser)
                )
                self.assertFalse(revocations[0].wait_drained(0))

        self.native.hook = hook
        with patch.object(web_named_transport, "_write_response") as write:
            self.refused()
        write.assert_not_called()
        self.assertTrue(revocations[0].wait_drained(0))

    def test_owner_generation_loss_refuses_after_native_page(self):
        def hook(target):
            if target == catalog._list_path(_PROJECT):
                self.observe.return_value = replace(self.identity, generation="e" * 32)

        self.native.hook = hook
        with patch.object(web_named_transport, "_write_response") as write:
            self.refused()
        write.assert_not_called()
        self.assertTrue(self.admission.wait_drained(0))

    def test_revocation_during_checked_write_is_close_only(self):
        revocations = []

        def partial_write(stream, _reply, _deadline, *, check):
            check()
            stream.sendall(b"HTTP/1.1 ")
            revocations.append(
                self.admission.begin_revocation(self.binding, self.browser)
            )
            self.assertFalse(revocations[0].wait_drained(0))
            check()

        with patch.object(
            web_named_transport, "_write_response", side_effect=partial_write
        ) as write:
            self.refused()
        write.assert_called_once()
        self.assertTrue(revocations[0].wait_drained(0))

    def test_uncertain_reap_fences_the_actual_use_and_app_without_reply(self):
        self.confirmed = False
        with patch.object(web_named_transport, "_write_response") as write:
            self.refused()
        write.assert_not_called()
        self.assertEqual(len(self.native.requests), 5)
        self.assertFalse(self.admission.wait_drained(0))
        self.assertIsNone(self.admission.lookup(self.binding, self.token))
        self.assertEqual(self.starts, 1)
        self.refused()
        self.assertEqual(self.starts, 1)


if __name__ == "__main__":
    unittest.main()
