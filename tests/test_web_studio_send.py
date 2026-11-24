from contextlib import contextmanager
from dataclasses import replace
import json
import os
import socket
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from oc_box import web_studio_send as studio

ID = "ses_studio_send"
PROJECT = "/worktree/repository/sub"
ORIGIN = "http://127.0.0.1:49876"
PASSWORD = "synthetic_control_password_" + "Z" * 24
TEXT = "Synthetic text\n🙂\tLiteral <tag>"
ROUTE = "/api/session/" + ID


def scope(**changes):
    return replace(
        studio.SendScope(
            "a" * 32, "b" * 32, "c" * 64, ORIGIN, PROJECT, "2.4.1", ID, 2, object()
        ),
        **changes,
    )


class FixtureChild:
    confirmed = True

    def __init__(self, closing):
        self.closing = closing

    def finish(self, deadline, check):
        check()

    def close(self):
        self.closing()


class FixtureNative:
    def __init__(self):
        self.requests = []
        self.info = {"version": "2.4.1", "pid": 42, "urls": [ORIGIN]}
        self.location = {
            "directory": PROJECT,
            "project": {
                "id": "verified",
                "directory": "/worktree/repository",
                "canonical": "/worktree/repository",
            },
        }
        self.target = {
            "id": ID,
            "projectID": "verified",
            "location": {"directory": PROJECT},
            "time": {"created": 1, "updated": 2},
            "subpath": "sub",
            "permissions": [
                {"action": "tool.execute", "resource": "*", "effect": "deny"}
            ],
        }
        self.active, self.inbox, self.moves = {}, [], []
        self.before_post = None
        self.after_target = lambda response: response
        self.reply = self.ack
        self.ack_headers = []
        self.ack_status = "200 OK"

    @staticmethod
    def ack(prompt):
        return {
            "data": {
                "id": "msg_studio_native",
                "sessionID": ID,
                "type": "user",
                "delivery": "queue",
                "time": {"created": 3},
                "payload": {"text": prompt["text"]},
            }
        }

    def serve(self, peer):
        with peer.makefile("rb") as stream:
            while line := stream.readline(4096):
                method, path, version = line.decode("ascii").strip().split(" ")
                assert version == "HTTP/1.1"
                headers = {}
                while (line := stream.readline(4096)) != b"\r\n":
                    if not line:
                        return
                    key, value = line.decode("ascii").strip().split(": ", 1)
                    headers[key] = value
                size = int(headers.get("Content-Length", "0"))
                body = stream.read(size) if size else None
                assert headers["Authorization"].startswith("Basic ")
                assert headers["Host"] == ORIGIN.removeprefix("http://")
                self.requests.append((method, path, body))
                if method == "POST":
                    assert path == ROUTE + "/prompt"
                    if self.before_post:
                        self.before_post()
                    status, extra = self.ack_status, self.ack_headers
                    value = self.reply(json.loads(body))
                elif path == "/api/info":
                    status, extra, value = "200 OK", [], self.info
                elif path == "/api/location":
                    status, extra, value = "200 OK", [], self.location
                elif path == ROUTE:
                    status, extra, value = (
                        "200 OK",
                        [],
                        {"data": self.after_target(self.target)},
                    )
                elif path.startswith(ROUTE + "/message?"):
                    status, extra, value = (
                        "200 OK",
                        [],
                        {
                            "data": self.moves,
                            "cursor": {"next": None, "previous": None},
                        },
                    )
                elif path == ROUTE + "/inbox":
                    status, extra, value = "200 OK", [], {"data": self.inbox}
                elif path == "/api/session/active":
                    status, extra, value = "200 OK", [], {"data": self.active}
                else:
                    raise AssertionError("Unexpected native route")
                payload = json.dumps(value, ensure_ascii=False).encode()
                lines = [
                    f"HTTP/1.1 {status}",
                    "Content-Type: application/json",
                    f"Content-Length: {len(payload)}",
                    "Connection: " + headers["Connection"],
                    *extra,
                ]
                peer.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + payload)
                if method == "POST":
                    peer.shutdown(socket.SHUT_WR)
                    return


@contextmanager
def fixture(native, *, selected=None, closing=lambda: None, deadline=None):
    if selected is None:
        selected = scope()
    live = [selected]
    errors = []
    with _controls() as controls:
        controller, control = controls
        grant = studio.StudioSend(
            selected,
            control=control,
            observe=lambda _: live[0],
            deadline=time.monotonic() + 2 if deadline is None else deadline,
        )
        threads = []

        def fake_start(send, _box):
            host, peer = socket.socketpair()
            peer.settimeout(3)

            def serve():
                with peer:
                    try:
                        native.serve(peer)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    except Exception as error:
                        errors.append(error)

            runner = Thread(target=serve)
            runner.start()
            threads.append(runner)
            send.track(host)
            send.register_workspace(FixtureChild(closing))

        with patch.object(studio.StudioSend, "_start", fake_start):
            try:
                yield grant, live, controller
            finally:
                grant.cancel()
                for runner in threads:
                    runner.join(timeout=3)
                    assert not runner.is_alive()
                if errors:
                    raise errors[0]


@contextmanager
def _controls():
    host, owner = socket.socketpair()
    with host, owner:
        yield host, owner


class StudioSendTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("OC_BOX_CONTAINER", None)

    def execute(self, grant, text=TEXT, password=PASSWORD):
        return grant.execute(None, native_password=password, text=text)

    def test_exact_fixed_write_uses_native_id_and_keeps_native_policy_untouched(self):
        native = FixtureNative()
        with fixture(native) as (grant, _, _):
            self.assertIs(self.execute(grant), studio.SendResult.QUEUED)
            self.assertTrue(grant.cleanup_confirmed)
            self.assertIs(self.execute(grant), studio.SendResult.REFUSED)
        writes = [item for item in native.requests if item[0] == "POST"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][1], ROUTE + "/prompt")
        self.assertEqual(
            json.loads(writes[0][2]),
            {"text": TEXT, "delivery": "queue", "resume": True},
        )
        self.assertEqual(native.requests[-2][:2], ("GET", ROUTE))
        self.assertEqual(native.target["permissions"][0]["effect"], "deny")

    def test_wrong_owner_browser_origin_session_and_selection_are_not_rebound(self):
        cases = {
            "workspace": {"workspace_id": "d" * 32},
            "owner": {"owner_generation": "d" * 32},
            "container": {"container_id": "d" * 64},
            "native origin": {"endpoint": "http://127.0.0.1:49001"},
            "project": {"project": "/elsewhere"},
            "selection": {"session_id": "ses_elsewhere"},
            "away and back": {"selection_generation": 3},
            "browser": {"browser": object()},
        }
        for label, change in cases.items():
            native, selected = FixtureNative(), scope()
            with (
                self.subTest(label=label),
                fixture(native, selected=selected) as (grant, live, _),
            ):
                live[0] = replace(selected, **change)
                self.assertIs(self.execute(grant), studio.SendResult.REFUSED)
                self.assertEqual(native.requests, [])
                live[0] = selected
                self.assertIs(self.execute(grant), studio.SendResult.REFUSED)

    def test_unsafe_native_preconditions_never_post(self):
        cases = (
            lambda n: n.info.update(version="2.4.2"),
            lambda n: n.info.update(urls=["http://127.0.0.1:49000"]),
            lambda n: n.location.update(directory="/other"),
            lambda n: n.target.update(projectID="unrelated"),
            lambda n: n.target.update(id="ses_wrong"),
            lambda n: n.target.update(location={"directory": PROJECT + "/other"}),
            lambda n: n.target.update(parentID="ses_parent"),
            lambda n: n.target.update(fork={"sessionID": "ses_parent"}),
            lambda n: n.target["time"].update(archived=9),
            lambda n: n.target.update(revert={"messageID": "msg_old"}),
            lambda n: n.moves.append({"type": "location-switched"}),
            lambda n: n.active.update({ID: {"type": "running"}}),
            lambda n: n.inbox.append({"type": "move"}),
        )
        for index, alter in enumerate(cases):
            native = FixtureNative()
            alter(native)
            with self.subTest(index=index), fixture(native) as (grant, _, _):
                self.assertIs(self.execute(grant), studio.SendResult.REFUSED)
                self.assertTrue(native.requests)
                self.assertTrue(all(method == "GET" for method, *_ in native.requests))

    def test_expired_cancelled_closed_or_invalid_input_never_starts_native_work(self):
        for text, password in (
            (" ", PASSWORD),
            ("é" * 2049, PASSWORD),
            ('"' * 4096, PASSWORD),
            ("\ud800", PASSWORD),
            ("two\x00parts", PASSWORD),
            (PASSWORD, PASSWORD),
            ({"text": TEXT}, PASSWORD),
            (TEXT, "short"),
        ):
            native = FixtureNative()
            with self.subTest(bad_type=type(text)), fixture(native) as (grant, _, _):
                self.assertIs(
                    self.execute(grant, text, password), studio.SendResult.REFUSED
                )
                self.assertEqual(native.requests, [])
        for stop in ("cancellation", "owner EOF", "expiry"):
            native = FixtureNative()
            with self.subTest(stop=stop), fixture(native) as (grant, _, controller):
                if stop == "cancellation":
                    grant.cancel()
                elif stop == "owner EOF":
                    controller.close()
                else:
                    grant._deadline = time.monotonic() - 1
                self.assertIs(self.execute(grant), studio.SendResult.REFUSED)
                self.assertEqual(native.requests, [])

    def test_observed_staged_revert_or_selection_change_on_last_get_refuses_write(self):
        for change in ("native revert", "selection"):
            native = FixtureNative()
            with self.subTest(change=change), fixture(native) as (grant, live, _):

                def target(response):
                    if (
                        len([path for _, path, _ in native.requests if path == ROUTE])
                        > 1
                    ):
                        if change == "native revert":
                            return {**response, "revert": {"messageID": "msg_before"}}
                        live[0] = replace(live[0], selection_generation=3)
                    return response

                native.after_target = target
                self.assertIs(self.execute(grant), studio.SendResult.REFUSED)
                self.assertFalse(
                    any(method == "POST" for method, *_ in native.requests)
                )

    def test_malformed_native_acknowledgment_means_uncertain_not_retry(self):
        for bad in (
            "wrong-session",
            "wrong-text",
            "native error",
            "duplicate length",
            "raw-extra",
        ):
            native = FixtureNative()
            if bad == "native error":
                native.ack_status = "409 Conflict"
            elif bad == "duplicate length":
                native.ack_headers = ["Content-Length: 4"]
            else:

                def reply(prompt):
                    result = FixtureNative.ack(prompt)
                    if bad == "wrong-session":
                        result["data"]["sessionID"] = "ses_foreign"
                    elif bad == "wrong-text":
                        result["data"]["payload"]["text"] = "other"
                    else:
                        result["data"]["payload"]["paths"] = ["not displayable"]
                    return result

                native.reply = reply
            with self.subTest(bad=bad), fixture(native) as (grant, _, _):
                self.assertIs(self.execute(grant), studio.SendResult.UNCONFIRMED)
                self.assertIs(self.execute(grant), studio.SendResult.REFUSED)
                self.assertEqual(sum(m == "POST" for m, *_ in native.requests), 1)

    def test_cancellation_interrupts_a_waiting_send_and_withholds_success(self):
        posted, release = Event(), Event()
        native = FixtureNative()

        def hold():
            posted.set()
            release.wait(timeout=3)

        native.before_post = hold
        with fixture(native) as (grant, _, _):
            outcomes = []
            runner = Thread(target=lambda: outcomes.append(self.execute(grant)))
            runner.start()
            try:
                self.assertTrue(posted.wait(timeout=1))
                grant.cancel()
                runner.join(timeout=1)
                self.assertFalse(runner.is_alive())
                self.assertEqual(outcomes, [studio.SendResult.UNCONFIRMED])
            finally:
                release.set()
                runner.join(timeout=2)

    def test_late_selection_and_cleanup_loss_never_return_queued(self):
        for stop in ("selection", "cancellation", "cleanup"):
            native = FixtureNative()

            def closing():
                if stop == "selection":
                    live[0] = replace(live[0], selection_generation=5)
                elif stop == "cancellation":
                    grant.cancel()
                else:
                    raise OSError("SYNTHETIC_RAW_NATIVE_ERROR")

            with (
                self.subTest(stop=stop),
                fixture(native, closing=closing) as (grant, live, _),
            ):
                self.assertIs(self.execute(grant), studio.SendResult.UNCONFIRMED)
                self.assertEqual(grant.cleanup_confirmed, stop != "cleanup")


if __name__ == "__main__":
    unittest.main()
