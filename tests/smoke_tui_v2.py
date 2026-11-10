"""Provider-free SessionCatalog checks against unmodified native OpenCode 2.2.6.

Run with TMPDIR=/tmp/opencode python3 -B tests/smoke_tui_v2.py
--opencode /path/to/opencode-v2. All databases/configuration and the stdio-owned
server are private synthetic state. Catalog requests use production
workspace_service.api and the real native CLI; only its Podman exec boundary is
replaced with local execution. No inference, shared service, host history or
Podman is used. This is native offline integration, NOT host/Podman lifecycle or
interactive TUI acceptance.
"""

import argparse
import contextlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from oc_box import oc_box as box
from oc_box import opencode_compat as compat
from oc_box import opencode_v2_io as bridge
from oc_box import tui_sessions as sessions
from oc_box import workspace_service
from smoke_session_sync_v2 import bounded, private_environment, seed


class FixtureFailure(AssertionError):
    """Fixed fixture diagnostics, never native errors or synthetic transcripts."""


def require(condition, message):
    if not condition:
        raise FixtureFailure(message)


def expect_refusal(reason, callback):
    try:
        callback()
    except sessions.CatalogError as error:
        require(
            error.reason == reason
            and str(error) == f"Session operation refused ({reason}).",
            "Catalog refusal did not retain its fixed category.",
        )
        return
    raise FixtureFailure("Catalog unexpectedly accepted a refused operation.")


@contextlib.contextmanager
def private_cwd(directory):
    # contextlib.chdir is not available on the supported Python 3.10 baseline.
    previous = Path.cwd()
    try:
        os.chdir(directory)
        yield
    finally:
        os.chdir(previous)


class NativeCLI:
    """Forward the production API parser to private native CLI subprocesses.

    The forwarding run uses production private memfd capture, checked exit codes
    and bounded execution. No response is mocked or rewritten. Observations hold
    only routes, synthetic IDs, page sizes and empty-success booleans, never CLI
    diagnostics.
    """

    def __init__(self, binary, env, directory, endpoint):
        self.binary, self.directory = binary, directory
        self.endpoint = endpoint
        self.env = {**env, "OPENCODE_PASSWORD": endpoint["password"]}
        self.calls, self.pages, self.empty_successes = [], [], []
        self.page_ids = []
        self.native_box = SimpleNamespace(CONTAINER_OPENCODE=binary, run=self.run)

    def client_command(self, _box, _data, connection, arguments):
        require(
            connection == self.endpoint
            and arguments[:2] == [self.binary, "api"]
            and arguments[4:6] == ["--server", self.endpoint["url"]]
            and len(arguments) in (6, 8)
            and (len(arguments) == 6 or arguments[6] == "--data"),
            "Native CLI did not retain its explicit private endpoint.",
        )
        return box.ContainerCommand(arguments, self.env)

    def run(self, command, **kwargs):
        require(
            kwargs.get("capture") is True and kwargs.get("private") is True,
            "Native CLI capture lost its private production transport.",
        )
        result = box.run(
            command, cwd=self.directory, stdin=subprocess.DEVNULL, **kwargs
        )
        if not result.stdout.strip():
            self.empty_successes.append((command[2].upper(), command[3]))
        return result

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        result = workspace_service.api(
            self.native_box, {}, self.endpoint, method, path, body, timeout=20
        )
        if method == "GET" and urlsplit(path).path == "/api/session":
            query = parse_qs(urlsplit(path).query)
            require(
                query.get("limit") == ["100"]
                and query.get("order") == ["asc"]
                and not {"parentID", "archived"} & query.keys(),
                "Catalog native inventory excluded children or archived sessions.",
            )
            self.pages.append(
                (
                    query,
                    len(result["data"]),
                    result["cursor"]["next"] is not None,
                )
            )
            self.page_ids.append({item["id"] for item in result["data"]})
        return result

    def mutation_calls(self):
        return [call for call in self.calls if call[0] in ("PATCH", "DELETE")]


def exported(request, ident):
    return request("GET", "/api/experimental/session/" + ident + "/export")["data"]


def exercise(binary, root):
    project, foreign = root / "project", root / "foreign"
    project.mkdir()
    foreign.mkdir()
    env = private_environment(root / "native")
    release = compat.version(bounded([binary, "--version"], env, project))
    require(release == "2.2.6", "This native contract fixture requires OpenCode 2.2.6.")
    print(
        "Native OpenCode " + release + "; private offline SessionCatalog fixture.",
        flush=True,
    )

    # Also change cwd for the server itself, not just its CLI clients. Config
    # discovery is disabled and the entire inherited environment is replaced.
    with (
        private_cwd(project),
        patch.dict(os.environ, env, clear=True),
        bridge.server(binary) as native,
    ):
        endpoint = native.endpoint
        cli = NativeCLI(binary, env, project, endpoint)
        with patch.object(workspace_service, "client_command", cli.client_command):
            catalog = sessions.SessionCatalog(cli, "fixture", "private-native")
            require(catalog.list() == [], "Private native inventory was not empty.")
            require(
                native("GET", "/api/credential")["data"] == [],
                "Private native fixture unexpectedly inherited credentials.",
            )
            require(
                cli("GET", "/api/session/active") == {"data": {}}
                and cli("GET", "/api/experimental/job") == {"data": []},
                "Native quiet-store activity/job route shapes changed.",
            )
            schema = native("GET", "/openapi.json")["paths"]
            mutations = schema["/api/session/{sessionID}"]
            require(
                all(
                    "204" in mutations[method]["responses"]
                    for method in ("patch", "delete")
                ),
                "Native session mutations no longer declare HTTP 204.",
            )

            parent = seed(native, "ses_tui_parent", project)
            child = seed(native, "ses_tui_child", foreign, parent=parent)
            grandchild = seed(native, "ses_tui_grandchild", project, parent=child)
            retained = seed(native, "ses_tui_foreign_root", foreign)
            stale = seed(native, "ses_tui_stale_selection", project)
            padding = {
                seed(native, f"ses_tui_padding_{index:03d}", project)
                for index in range(101)
            }
            expected = padding | {parent, child, grandchild, retained, stale}
            for ident in (child, retained):
                require(
                    native("PATCH", "/api/session/" + ident, {"archived": True})
                    is None,
                    "Native fixture archive setup was not a no-content response.",
                )
            retained_before = exported(native, retained)

            cli.pages.clear()
            cli.page_ids.clear()
            inventory = catalog.list()
            # Native 2.2.6 returns a cursor even after the short data page;
            # only the following empty page terminates this actual inventory.
            require(
                {item["id"] for item in inventory} == expected
                and len(cli.pages) == 3
                and [(size, more) for _query, size, more in cli.pages]
                == [(100, True), (len(expected) - 100, True), (0, False)]
                and "cursor" not in cli.pages[0][0]
                and all(query.get("cursor") for query, _size, _more in cli.pages[1:])
                and {child, retained} <= cli.page_ids[1],
                "Catalog did not exhaust real native global cursor pagination.",
            )
            by_id = {item["id"]: item for item in inventory}
            require(
                by_id[child]["parentID"] == parent
                and by_id[grandchild]["parentID"] == child
                and "archived" in by_id[child]["time"]
                and "archived" in by_id[retained]["time"]
                and {item["location"]["directory"] for item in inventory}
                == {str(project), str(foreign)},
                "Global native inventory lost a directory, child or archived session.",
            )
            require(
                all(
                    not {"metadata", "permissions", "cost", "tokens"} & item.keys()
                    for item in inventory
                ),
                "Catalog leaked non-allowlisted native session fields.",
            )
            require(
                sessions.normalize_session(cli("GET", "/api/session/" + parent)["data"])
                == by_id[parent],
                "Native session GET envelope differs from catalog snapshots.",
            )
            cli.pages.clear()
            cli.page_ids.clear()
            scoped = catalog.list(str(project))
            require(
                {item["id"] for item in scoped} == expected - {child, retained}
                and [(size, more) for _query, size, more in cli.pages]
                == [(100, True), (len(scoped) - 100, True), (0, False)],
                "Catalog did not exhaust real directory-scoped cursor pagination.",
            )
            require(
                {item["id"] for item in catalog.list(str(foreign))}
                == {child, retained},
                "Foreign-directory listing omitted an archived child or root.",
            )
            print(
                "PASS: 106 native sessions; real global/scoped 100-item pagination, two directories, archived roots/children and allowlisted GET metadata.",
                flush=True,
            )

            # Unknown ownership must refuse even though the native process is
            # demonstrably quiet. Neither refusal may issue a mutation request.
            catalog.list()
            before_mutations = cli.mutation_calls()
            expect_refusal("ownership-unknown", lambda: catalog.archive(parent, True))
            expect_refusal("ownership-unknown", lambda: catalog.prepare_delete(parent))
            require(
                cli.mutation_calls() == before_mutations
                and catalog.list() == inventory,
                "Unknown-ownership refusal changed native session state.",
            )
            print(
                "PASS: native quiet-store unknown-ownership archive/delete refusals are nonmutating.",
                flush=True,
            )

            ownership_calls = []

            def ownership(origin, store, identities):
                require(
                    native.alive()
                    and (origin, store) == ("fixture", "private-native")
                    and bool(identities)
                    and set(identities) <= expected,
                    "Ownership probe was not bound to known private synthetic state.",
                )
                ownership_calls.append(identities)
                return "process-local"

            catalog = sessions.SessionCatalog(
                cli, "fixture", "private-native", ownership=ownership
            )
            catalog.list()
            require(
                catalog.archive(parent, True) is None,
                "Catalog archive did not accept native CLI no-content success.",
            )
            archived = {item["id"]: item for item in catalog.list()}
            require(
                "archived" in archived[parent]["time"]
                and "archived" in archived[child]["time"]
                and "archived" not in archived[grandchild]["time"]
                and ownership_calls == [(parent,)],
                "Native archive did not affect only the selected session.",
            )
            require(
                catalog.archive(parent, False) is None,
                "Catalog unarchive did not accept native CLI no-content success.",
            )
            unarchived = {item["id"]: item for item in catalog.list()}
            require(
                "archived" not in unarchived[parent]["time"]
                and "archived" in unarchived[child]["time"]
                and all(
                    unarchived[ident] == by_id[ident] for ident in expected - {parent}
                )
                and cli.empty_successes == [("PATCH", "/api/session/" + parent)] * 2,
                "Native archive/unarchive failed to preserve children or empty CLI stdout.",
            )
            print(
                "PASS: native CLI PATCH 204 archive/unarchive returns None; GET snapshots and quiet-store checks use real envelopes.",
                flush=True,
            )

            terminal = native(
                "POST",
                "/api/experimental/session/" + child + "/terminal",
                {
                    "command": "/bin/sh",
                    "args": ["-c", "sleep 30"],
                    "cwd": str(foreign),
                    "title": "Synthetic delete guard",
                    "env": {},
                    "size": {"cols": 80, "rows": 24},
                },
            )["data"]
            try:
                catalog.list()
                before_mutations = cli.mutation_calls()
                expect_refusal(
                    "session-terminal-running", lambda: catalog.prepare_delete(parent)
                )
                require(
                    cli.mutation_calls() == before_mutations,
                    "Running terminal refusal issued a native session mutation.",
                )
            finally:
                native("DELETE", "/api/experimental/persistent-pty/" + terminal["id"])
            catalog.list()
            print(
                "PASS: a real running persistent shell in a cross-directory child refuses deletion without a session mutation.",
                flush=True,
            )

            preview = catalog.prepare_delete(parent)
            require(
                preview["id"] == parent
                and preview["origin"] == "fixture"
                and preview["store"] == "private-native"
                and preview["descendant_count"] == 2
                and [item["id"] for item in preview["sessions"]]
                == [parent, child, grandchild]
                and preview["ownership"] == "process-local"
                and preview["warning"] == sessions.PROCESS_LOCAL_WARNING,
                "Native recursive-delete preview lost its cross-directory archived tree or warning.",
            )
            require(
                catalog.delete(preview) is None,
                "Catalog delete did not accept native CLI no-content success.",
            )
            expected -= {parent, child, grandchild}
            after_delete = catalog.list()
            require(
                {item["id"] for item in after_delete} == expected
                and exported(native, retained) == retained_before
                and cli.empty_successes[-1] == ("DELETE", "/api/session/" + parent)
                and (parent, child, grandchild) in ownership_calls,
                "Native recursive delete omitted descendants or changed an independent foreign root.",
            )
            before_calls = len(cli.calls)
            expect_refusal("invalid-preview", lambda: catalog.delete(preview))
            require(
                len(cli.calls) == before_calls,
                "Consumed delete preview contacted native state.",
            )
            print(
                "PASS: native CLI DELETE 204 recursively removes archived cross-directory descendants, retains independent roots and consumes confirmation.",
                flush=True,
            )

            # The selection disappears on the native side. Its failed real CLI
            # GET must become a fixed catalog error, never private native stderr.
            native("DELETE", "/api/session/" + stale)
            before_mutations = cli.mutation_calls()
            expect_refusal("request-failed", lambda: catalog.archive(stale, True))
            expected.remove(stale)
            require(
                cli.mutation_calls() == before_mutations
                and {item["id"] for item in catalog.list()} == expected
                and exported(native, retained) == retained_before,
                "Failed native selection lookup issued a mutation or changed retained state.",
            )
            require(
                cli("GET", "/api/session/active") == {"data": {}}
                and cli("GET", "/api/experimental/job") == {"data": []},
                "Provider-free native fixture left activity or job drains.",
            )
            print(
                "PASS: failed native CLI GET is a fixed request-failed refusal; 102 independent synthetic sessions remain and activity/jobs are empty.",
                flush=True,
            )


def main(binary):
    binary = str(Path(binary).resolve())
    try:
        with tempfile.TemporaryDirectory(prefix="oc-box-tui-native-v2-") as temporary:
            exercise(binary, Path(temporary))
    except FixtureFailure:
        raise
    except Exception:  # noqa: BLE001 -- Never serialize native diagnostics.
        raise FixtureFailure(
            "Native V2 SessionCatalog fixture failed; private diagnostics were suppressed."
        ) from None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True)
    args = parser.parse_args()
    try:
        main(args.opencode)
    except FixtureFailure as error:
        print("FAIL: " + str(error), file=sys.stderr)
        raise SystemExit(1) from None
