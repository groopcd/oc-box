import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from oc_box import oc_box as box, web_history as history


DIRECTORY = "/container/workspace & é"
PASSWORD = "synthetic_owner_" + "p" * 32
PRIVATE = "SYNTHETIC_PRIVATE_NATIVE_DATA"
AUTH = "Basic " + base64.b64encode(("opencode:" + PASSWORD).encode()).decode()
REAL_POPEN = subprocess.Popen


def native(ident="ses_history", directory=DIRECTORY, **fields):
    return {
        "id": ident,
        "projectID": "synthetic-project-id",
        "cost": 0,
        "tokens": {
            "input": 0,
            "output": 0,
            "reasoning": 0,
            "cache": {"read": 0, "write": 0},
        },
        "location": {"directory": directory},
        "title": "A synthetic <history> title",
        "time": {"created": 1, "updated": 2},
        "metadata": {"opaque": PRIVATE, "auth": PASSWORD},
        "permissions": [{"action": "*", "resource": PRIVATE, "effect": "deny"}],
        **fields,
    }


def page(items=(), next_cursor=None, previous=None):
    return {"data": list(items), "cursor": {"next": next_cursor, "previous": previous}}


def encoded(value):
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


class HistoryTestCase(unittest.TestCase):
    def assert_refused(self, operation):
        with self.assertRaises(history.CatalogError) as error:
            operation()
        self.assertEqual(
            str(error.exception), "Workspace history could not be verified."
        )
        self.assertTrue(error.exception.__suppress_context__)


class CatalogTest(HistoryTestCase):
    def test_selected_history_id_rechecks_exact_directory_and_native_identity(self):
        fetch = Mock(return_value=encoded({"data": native("ses_root")}))
        result = history._selected_row(fetch, DIRECTORY, "ses_root")
        self.assertEqual(result["id"], "ses_root")
        fetch.assert_called_once_with("/api/session/ses_root", history.MAX_PAGE_BYTES)
        for row in (
            native("ses_root", directory=Path(DIRECTORY).with_name("moved").as_posix()),
            native("ses_different"),
        ):
            with self.subTest(row=row["id"]):
                fetch.return_value = encoded({"data": row})
                self.assert_refused(
                    lambda: history._selected_row(fetch, DIRECTORY, "ses_root")
                )

    def test_cursor_walk_preserves_exact_directory_and_projects_only_display_fields(
        self,
    ):
        cursor = "opaque+/=?&directory=/outside#é"
        root = native("ses_root", time={"created": 1, "updated": 5, "busy": 0})
        child = native(
            "ses_child",
            parentID="ses_moved_outside",
            time={"created": 1, "updated": 4, "archived": 3},
            title="Child\nname\u009b",
            body=PRIVATE,
            messages=[{"text": PRIVATE}],
        )
        fetch = Mock(
            side_effect=[
                encoded(page([root], cursor, "back")),
                encoded(page([], "third")),
                encoded(page([child], "last", "back3")),
                encoded(page()),  # full last page can require an empty sentinel
            ]
        )

        self.assertEqual(
            history._inventory(fetch, DIRECTORY),
            [
                {
                    "id": "ses_root",
                    "title": "A synthetic <history> title",
                    "time": {"created": 1, "updated": 5},
                    "isChild": False,
                },
                {
                    "id": "ses_child",
                    "title": "Child name",
                    "time": {"created": 1, "updated": 4, "archived": 3},
                    "isChild": True,
                },
            ],
        )
        for call, wanted in zip(fetch.call_args_list, [None, cursor, "third", "last"]):
            url, maximum = call.args
            parts = urlsplit(url)
            self.assertEqual(
                (parts.scheme, parts.netloc, parts.path, parts.fragment),
                ("", "", "/api/session", ""),
            )
            expected = {
                "directory": [DIRECTORY],
                "order": ["desc"],
                "limit": [str(history.PAGE_SIZE)],
            }
            if wanted is not None:
                expected["cursor"] = [wanted]
            self.assertEqual(parse_qs(parts.query), expected)
            self.assertEqual(maximum, history.MAX_PAGE_BYTES)

    def test_empty_requires_explicit_native_exhaustion(self):
        fetch = Mock(return_value=encoded(page()))
        self.assertEqual(history._inventory(fetch, DIRECTORY), [])
        fetch.assert_called_once()
        for no_next in ({}, {"previous": None}):
            with self.subTest(no_next=no_next):
                self.assert_refused(
                    lambda: history._inventory(
                        Mock(return_value=encoded({"data": [], "cursor": no_next})),
                        DIRECTORY,
                    )
                )

    def test_out_of_scope_or_malformed_late_page_withholds_every_row(self):
        base = page([native("ses_first")], "cursor")
        invalid = {
            "unfiltered path": page([native(directory="/other")]),
            "sibling prefix": page([native(directory=DIRECTORY + "-sibling")]),
            "nested path": page([native(directory=DIRECTORY + "/sub")]),
            "host side path": page([native(directory="/host/restore-destination")]),
            "duplicate ID": page([native("ses_first")]),
            "cursor loop": page([], "cursor"),
            "empty cursor": page([], ""),
            "oversized cursor": page([], "é" * history.MAX_CURSOR_BYTES),
            "absolute parent path": page([native(parentID="/outside")]),
            "self parent": page([native(parentID="ses_history")]),
            "malformed project": page([native(projectID=None)]),
            "malformed title": page([native(title={})]),
            "malformed timestamp": page([native(time={"created": 1, "updated": True})]),
            "corrupt archive metadata": page(
                [native(time={"created": 1, "updated": 2, "archived": -1})]
            ),
            "non-list": {"data": {}, "cursor": {"next": None}},
        }
        for name, reply in invalid.items():
            with self.subTest(name=name):
                fetch = Mock(side_effect=[encoded(base), encoded(reply)])
                self.assert_refused(lambda: history._inventory(fetch, DIRECTORY))
                self.assertEqual(fetch.call_count, 2)

    def test_page_count_row_count_and_running_byte_budget(self):
        first = encoded(page([native("ses_first")], "cursor"))
        last = encoded(page([native("ses_second")]))
        for name, limits, payloads, calls in (
            ("pages", {"MAX_PAGES": 1}, [first, last], 1),
            ("rows", {"MAX_ROWS": 1}, [first, last], 2),
            (
                "rows per page",
                {"PAGE_SIZE": 1},
                [encoded(page([native(), native("ses_other")]))],
                1,
            ),
            ("bytes per page", {"MAX_PAGE_BYTES": len(first) - 1}, [first], 1),
            (
                "total bytes",
                {"MAX_TOTAL_BYTES": len(first) + len(last) - 1},
                [first, last],
                2,
            ),
        ):
            with self.subTest(name=name), patch.multiple(history, **limits):
                fetch = Mock(side_effect=payloads)
                self.assert_refused(lambda: history._inventory(fetch, DIRECTORY))
                self.assertEqual(fetch.call_count, calls)
                if name == "total bytes":
                    self.assertEqual(fetch.call_args.args[1], len(last) - 1)
        with patch.object(history, "MAX_TOTAL_BYTES", len(first) + len(last)):
            self.assertEqual(
                len(history._inventory(Mock(side_effect=[first, last]), DIRECTORY)), 2
            )

    def test_clipped_unicode_title_is_valid_and_empty_title_has_identity_fallback(self):
        title = "a" + "🦉" * history.MAX_TITLE_BYTES
        fetch = Mock(
            return_value=encoded(
                page([native(title=title), native("ses_blank", title="\n")])
            )
        )
        first, second = history._inventory(fetch, DIRECTORY)
        self.assertEqual(
            first["title"], "a" + "🦉" * ((history.MAX_TITLE_BYTES - 1) // 4)
        )
        self.assertEqual(second["title"], "ses_blank")
        self.assertEqual(
            history._display_catalog(encoded([first, second])), [first, second]
        )

    def test_catalog_refuses_bad_binding_before_spawn(self):
        data = {
            "id": "a" * 32,
            "project": "/host/different",
            "container_project": DIRECTORY,
        }
        endpoint = {
            "workspace": data["id"],
            "url": "http://127.0.0.1:1",
            "password": PASSWORD,
        }
        with patch.object(history, "_read_bridge") as spawn:
            for altered in (
                {"workspace": "b" * 32},
                {"url": "https://127.0.0.1:1"},
                {"url": "http://127.0.0.1:1/api/session"},
                {"url": "http://remote.invalid:1"},
                {"url": "http://127.0.0.1:01"},
                {"password": "short"},
            ):
                with self.subTest(altered=altered):
                    self.assert_refused(
                        lambda: history.catalog(box, data, {**endpoint, **altered})
                    )
            for directory in (
                "/",
                "//global",
                "/a/../b",
                "/a//b",
                "/a/./b",
                "/a/",
                "relative",
                "/new\nline",
            ):
                with self.subTest(directory=directory):
                    record = {**data, "container_project": directory}
                    self.assert_refused(lambda: history.catalog(box, record, endpoint))
            spawn.assert_not_called()


@contextmanager
def loopback_fixture(responses):
    pending = iter(responses)
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(
                (
                    self.path,
                    self.headers["Authorization"],
                    self.headers["Accept-Encoding"],
                )
            )
            status, headers, payload = next(pending, (500, {}, b""))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, str(value))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        thread.start()
        try:
            yield "http://127.0.0.1:" + str(server.server_port), calls
        finally:
            server.shutdown()
            thread.join(timeout=2)


def response(payload, **headers):
    return (
        200,
        {"Content-Type": "application/json", "Content-Length": len(payload), **headers},
        payload,
    )


class TransportTest(HistoryTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-web-history-")
        self.addCleanup(temporary.cleanup)
        self.directory = str(Path(temporary.name) / "project & é")
        Path(self.directory).mkdir()
        self.data = {
            "id": "b" * 32,
            "project": "/host/synthetic-restored",
            "container_project": self.directory,
            "container": "synthetic-workspace-container",
            "home": "/container/synthetic-private-home",
        }

    def bridge(self, command, **kwargs):
        # Replace only Podman with a local process. Execute the unmodified
        # providerless client trampoline and standalone worker over real pipes.
        marker = command.index(self.data["container"])
        self.assertEqual(
            command[:marker],
            [
                "podman",
                "exec",
                "-i",
                "--workdir",
                self.directory,
                "--env",
                "OPENCODE_PASSWORD",
            ],
        )
        self.assertNotIn(PASSWORD, " ".join(command))
        self.assertNotIn("SYNTHETIC_PROVIDER_SECRET", kwargs["env"])
        self.assertEqual(kwargs["env"]["OPENCODE_PASSWORD"], PASSWORD)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        return REAL_POPEN(command[marker + 1 :], cwd=self.directory, **kwargs)

    def catalog(self, url):
        endpoint = {"workspace": self.data["id"], "url": url, "password": PASSWORD}
        with (
            patch.object(history.subprocess, "Popen", side_effect=self.bridge),
            patch.dict(os.environ, {"SYNTHETIC_PROVIDER_SECRET": PRIVATE}),
        ):
            return history.catalog(box, self.data, endpoint)

    def test_scoped_providerless_worker_over_real_authentication_and_capture(self):
        row = native(
            directory=self.directory, metadata={"auth": PASSWORD, "blob": PRIVATE}
        )
        responses = [response(encoded(page([row], "last"))), response(encoded(page()))]
        with loopback_fixture(responses) as (url, calls):
            result = self.catalog(url)
        self.assertEqual([item["id"] for item in result], [row["id"]])
        self.assertEqual(set(result[0]), {"id", "title", "time", "isChild"})
        self.assertNotIn(PRIVATE, repr(result))
        self.assertNotIn(PASSWORD, repr(result))
        self.assertEqual(len(calls), 2)
        for index, (path, auth, encoding) in enumerate(calls):
            target = urlsplit(path)
            self.assertEqual(target.path, "/api/session")
            query = parse_qs(target.query)
            self.assertEqual(query["directory"], [self.directory])
            self.assertEqual(query.get("cursor"), ["last"] if index else None)
            self.assertEqual((auth, encoding), (AUTH, "identity"))

    def test_bad_wire_replies_after_valid_page_never_escape_worker(self):
        first = response(encoded(page([native(directory=self.directory)], "next")))
        for label, bad in (
            ("redirect", (302, {"Location": "http://127.0.0.1:1/"}, b"")),
            ("native refusal", (401, {}, PRIVATE.encode())),
            ("truncated HTTP", response(encoded(page()), **{"Content-Length": 500})),
            (
                "oversized HTTP",
                response(b"", **{"Content-Length": history.MAX_PAGE_BYTES + 1}),
            ),
            (
                "compressed HTTP",
                response(encoded(page()), **{"Content-Encoding": "gzip"}),
            ),
            (
                "ambiguous HTTP",
                response(encoded(page()), **{"Transfer-Encoding": "chunked"}),
            ),
            (
                "duplicate JSON field",
                response(b'{"data":[],"data":[],"cursor":{"next":null}}'),
            ),
            ("nonfinite JSON", response(b'{"data":[NaN],"cursor":{"next":null}}')),
            ("partial JSON", response(b'{"data":[]')),
            (
                "foreign scope",
                response(encoded(page([native(directory="/host/other")]))),
            ),
            (
                "credential straddles display clip",
                response(
                    encoded(
                        page(
                            [
                                native(
                                    "ses_other",
                                    self.directory,
                                    title="a" * (history.MAX_TITLE_BYTES - 8)
                                    + PASSWORD,
                                )
                            ]
                        )
                    )
                ),
            ),
        ):
            with (
                self.subTest(label=label),
                loopback_fixture([first, bad]) as (url, calls),
            ):
                self.assert_refused(lambda: self.catalog(url))
                self.assertEqual(len(calls), 2)

    def test_rejects_untrusted_bridge_output_and_caps_stdout_during_read(self):
        row = {
            "id": "ses_valid",
            "title": "Title",
            "time": {"created": 1, "updated": 2},
            "isChild": False,
        }
        for malformed in (
            [{**row, "body": PRIVATE}],
            [{**row, "isChild": 1}],
            [row, row],
        ):
            with self.subTest(malformed=malformed):
                self.assert_refused(
                    lambda: history._display_catalog(encoded(malformed))
                )
        command = box.ContainerCommand(
            [sys.executable, "-I", "-S", "-c", "import os; os.write(1, b'x' * 8192)"],
            {},
        )
        with patch.object(history, "MAX_OUTPUT_BYTES", 32):
            self.assert_refused(lambda: history._read_bridge(command))
        command = box.ContainerCommand(
            [sys.executable, "-I", "-S", "-c", "print('[]'); raise SystemExit(2)"], {}
        )
        self.assert_refused(lambda: history._read_bridge(command))

    def test_valid_output_without_process_completion_is_not_a_catalog(self):
        command = box.ContainerCommand(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                "import os, time; os.write(1, b'[]'); time.sleep(5)",
            ],
            {},
        )
        processes = []

        def launch(*args, **kwargs):
            child = REAL_POPEN(*args, **kwargs)
            processes.append(child)
            return child

        with (
            patch.object(history.subprocess, "Popen", side_effect=launch),
            patch.object(history, "HOST_TIMEOUT", 0.2),
        ):
            self.assert_refused(lambda: history._read_bridge(command))
        self.assertIsNotNone(processes[0].returncode)


if __name__ == "__main__":
    unittest.main()
