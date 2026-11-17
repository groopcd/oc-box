"""Cooperative native TUI selection: metadata, never permissions or attestation.

The public consumer returns Snapshot(status, client, selected, tabs, activeRoot,
source, reason). Only a fresh owner-TUI report, or exactly one fresh alternate
TUI report, is usable. Native GET verifies every ID and the selected ancestry in
the same private service; viewed times, drains, tab order and history are never
selection heuristics. Caller must hold the existing validated private lease.

This file is also the stdlib-only, stdin-fed container read helper. Passwords
arrive only in its existing client environment, never settings/argv or output.
"""

import base64
from dataclasses import asdict, dataclass
from http.client import HTTPConnection
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import quote, urlsplit

SUPPORTED = frozenset(("2.2.6", "2.2.7", "2.3.0"))
MAX_TABS = 4096
MAX_REPLY = 1024 * 1024
MAX_ANCESTRY = 32
RPC = "/api/rpc/oc-box.ui-state/"
RESOURCES = (
    "ui_state_server.js",
    "ui_state_tui.js",
    "ui_state_rpc.js",
    "ui_state_memory.js",
)


@dataclass(frozen=True)
class Snapshot:
    status: str
    client: str = None
    selected: str = None
    tabs: tuple = ()
    activeRoot: str = None
    source: str = "unknown"
    reason: str = None


class StateError(ValueError):
    """Fixed content-free categories only."""


def _nonce(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _id(value):
    return (
        isinstance(value, str)
        and len(value) <= 128
        and re.fullmatch(r"ses[A-Za-z0-9_-]+", value) is not None
    )


def package_sources(package):
    """Generated private container package; never write a host plugin/config."""
    manifest = {
        "name": "oc-box-ui-state",
        "type": "module",
        "exports": {
            ".": "./ui_state_server.js",
            "./tui": "./ui_state_tui.js",
            "./rpc": "./ui_state_rpc.js",
        },
    }
    return {
        "package.json": (json.dumps(manifest) + "\n").encode(),
        **{name: (Path(package) / name).read_bytes() for name in RESOURCES},
    }


def _output(value):
    if not isinstance(value, dict) or set(value) != {"output"}:
        raise StateError("invalid-response")
    return value["output"]


def _metadata(connection):
    value = connection.get("ui_state")
    if value is None or value == {"available": False}:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"available", "generation", "ownerClient"}
        or value["available"] is not True
        or not _nonce(value["generation"])
        or not _nonce(value["ownerClient"])
    ):
        raise StateError("invalid-binding")
    return value


def _target(method, directory):
    return RPC + method + "?location%5Bdirectory%5D=" + quote(directory, safe="")


def available(box, data, connection, *, timeout=4):
    """Owning-start-only, read-only readiness probe; no retry or installation."""
    if data.get("opencode_version") not in SUPPORTED:
        return False
    try:
        deadline = time.monotonic() + timeout
        # Native V2's config/provider plugins activate in stages. Resolve its
        # public location readiness boundary before asking for a custom RPC,
        # as normal owning runtime preparation already does. No inference.
        box.workspace_service.api(
            box,
            data,
            connection,
            "GET",
            "/api/model/default?location%5Bdirectory%5D="
            + quote(box.container_project(data), safe=""),
            timeout=timeout,
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        return _output(
            box.workspace_service.api(
                box,
                data,
                connection,
                "POST",
                _target("ping", box.container_project(data)),
                {"input": {}},
                timeout=remaining,
            )
        ) == {"protocol": 1}
    except (
        box.BoxError,
        StateError,
        OSError,
        ValueError,
        TypeError,
        subprocess.SubprocessError,
    ):
        return False


def _report(value, metadata):
    if (
        not isinstance(value, dict)
        or type(value.get("protocol")) is not int
        or value["protocol"] != 1
    ):
        raise StateError("invalid-response")
    if value.get("status") == "missing":
        if value.get("reason") not in (
            "overflow",
            "generation",
            "ambiguous",
            "absent",
            "reported-unknown",
        ):
            raise StateError("invalid-response")
        return Snapshot("missing", reason=value["reason"])
    expected = {
        "protocol",
        "status",
        "epoch",
        "generation",
        "client",
        "seq",
        "routeType",
        "selected",
        "tabsEnabled",
        "tabs",
        "activeRoot",
        "focus",
        "source",
    }
    if (
        set(value) != expected
        or value["status"] != "valid"
        or value["generation"] != metadata["generation"]
        or not isinstance(value["epoch"], str)
        or not 1 <= len(value["epoch"]) <= 64
        or not _nonce(value["client"])
        or type(value["seq"]) is not int
        or not 1 <= value["seq"] <= 2**53 - 1
        or value["routeType"] not in ("home", "session", "plugin")
        or value["focus"] not in ("unknown", "focused", "blurred")
        or type(value["tabsEnabled"]) is not bool
        or not isinstance(value["tabs"], list)
        or len(value["tabs"]) > MAX_TABS
        or any(not _id(ident) for ident in value["tabs"])
        or len(set(value["tabs"])) != len(value["tabs"])
        or (value["selected"] is not None and not _id(value["selected"]))
        or (value["activeRoot"] is not None and not _id(value["activeRoot"]))
        or ((value["routeType"] == "session") != (value["selected"] is not None))
        or value["source"] not in ("owner-tui", "single-tui")
        or (
            (value["source"] == "owner-tui")
            != (value["client"] == metadata["ownerClient"])
        )
    ):
        raise StateError("invalid-response")
    if not value["tabsEnabled"]:
        if value["tabs"] or value["activeRoot"] is not None:
            raise StateError("invalid-response")
    elif value["routeType"] == "session":
        if value["activeRoot"] not in value["tabs"]:
            raise StateError("invalid-response")
    if value["routeType"] != "session" and value["activeRoot"] is not None:
        raise StateError("invalid-response")
    return Snapshot(
        "valid",
        value["client"],
        value["selected"],
        tuple(value["tabs"]),
        value["activeRoot"],
        value["source"],
    )


def verified_snapshot(settings, request):
    """request(method, route, body) is one explicit existing native HTTP client."""
    metadata = settings["metadata"]
    query = {
        "input": {
            "generation": metadata["generation"],
            "ownerClient": metadata["ownerClient"],
        }
    }
    target = _target("snapshot", settings["directory"])
    first = _output(request("POST", target, query))
    snapshot = _report(first, metadata)
    if snapshot.status != "valid":
        return snapshot
    cached = {}

    def get(ident):
        if ident not in cached:
            result = request("GET", "/api/session/" + ident, None)
            info = result.get("data") if isinstance(result, dict) else None
            if (
                not isinstance(info, dict)
                or info.get("id") != ident
                or not isinstance(info.get("projectID"), str)
                or not info["projectID"]
                or not isinstance(info.get("location"), dict)
                or not isinstance(info["location"].get("directory"), str)
                or not info["location"]["directory"].startswith("/")
                or (info.get("parentID") is not None and not _id(info["parentID"]))
            ):
                raise StateError("invalid-session")
            cached[ident] = info
        return cached[ident]

    for ident in snapshot.tabs:
        if get(ident).get("parentID") is not None:
            raise StateError("non-root-tab")
    if snapshot.selected is not None:
        ident, seen = snapshot.selected, set()
        for _ in range(MAX_ANCESTRY):
            if ident in seen:
                raise StateError("invalid-ancestry")
            seen.add(ident)
            info = get(ident)
            parent = info.get("parentID")
            if parent is None:
                if first["tabsEnabled"] and ident != snapshot.activeRoot:
                    raise StateError("selected-root-mismatch")
                break
            ident = parent
        else:
            raise StateError("invalid-ancestry")
    second = _output(request("POST", target, query))
    if _report(second, metadata) != snapshot or any(
        first.get(key) != second.get(key)
        for key in ("epoch", "generation", "routeType", "tabsEnabled")
    ):
        return Snapshot("missing", reason="changed")
    return snapshot


def read_snapshot(box, data, connection, *, timeout=4):
    """Read-only private-lease consumer; absence never elects/refreshes a server."""
    try:
        if (
            type(timeout) not in (int, float)
            or not 0 < timeout <= 30
            or not math.isfinite(timeout)
        ):
            raise StateError("transport")
        if not isinstance(connection, dict) or not isinstance(data, dict):
            raise StateError("invalid-binding")
        metadata = _metadata(connection)
        if metadata is None:
            return Snapshot("missing", reason="unavailable")
        if data.get("opencode_version") not in SUPPORTED:
            return Snapshot("missing", reason="unsupported-version")
        if connection.get("workspace") != data.get("id"):
            raise StateError("invalid-binding")
        box.workspace_service.validate_endpoint(connection)
        settings = {
            "url": connection["url"],
            "directory": box.container_project(data),
            "metadata": metadata,
            "timeout": timeout,
        }
        command = box.workspace_service.client_command(
            box,
            data,
            connection,
            ["python3", "-I", "-S", "-B", "-c", Path(__file__).read_text()],
        )
        result = box.run(
            command,
            input=json.dumps(settings),
            capture=True,
            private=True,
            check=False,
            timeout=timeout,
        )
        if result.returncode or len(result.stdout.encode()) > MAX_REPLY:
            raise StateError("transport")
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or set(value) != set(
            asdict(Snapshot("missing"))
        ):
            raise StateError("transport")
        if not isinstance(value["tabs"], list):
            raise StateError("transport")
        value["tabs"] = tuple(value["tabs"])
        answer = Snapshot(**value)
        if answer.status not in ("valid", "missing", "invalid"):
            raise StateError("transport")
        # The container verifies complete native metadata; host checks protocol
        # structure again without trusting arbitrary strings in diagnostics.
        if answer.status == "valid":
            if (
                not _nonce(answer.client)
                or answer.source not in ("owner-tui", "single-tui")
                or answer.reason is not None
                or (
                    (answer.source == "owner-tui")
                    != (answer.client == metadata["ownerClient"])
                )
                or any(not _id(ident) for ident in answer.tabs)
                or len(answer.tabs) > MAX_TABS
                or len(set(answer.tabs)) != len(answer.tabs)
                or (answer.selected is not None and not _id(answer.selected))
                or (
                    answer.activeRoot is not None
                    and answer.activeRoot not in answer.tabs
                )
                or (answer.selected is None and answer.activeRoot is not None)
                or (
                    answer.selected is not None
                    and answer.tabs
                    and answer.activeRoot is None
                )
            ):
                raise StateError("transport")
        elif (
            answer.client is not None
            or answer.selected is not None
            or answer.tabs
            or answer.activeRoot is not None
            or answer.source != "unknown"
            or answer.reason not in REASONS
        ):
            raise StateError("transport")
        return answer
    except StateError as error:
        return Snapshot("invalid", reason=str(error))
    except (box.BoxError, OSError, ValueError, TypeError, subprocess.SubprocessError):
        return Snapshot("missing", reason="transport")


REASONS = frozenset(
    (
        "unavailable",
        "unsupported-version",
        "transport",
        "invalid-binding",
        "invalid-response",
        "invalid-session",
        "non-root-tab",
        "invalid-ancestry",
        "selected-root-mismatch",
        "changed",
        "overflow",
        "generation",
        "ambiguous",
        "absent",
        "reported-unknown",
        "native-400",
        "native-401",
        "native-403",
        "native-404",
        "native-409",
        "native-500",
        "native-503",
        "rpc-invalid-input",
        "rpc-invalid-output",
    )
)


def main():
    settings = json.loads(sys.stdin.buffer.read(16385))
    parsed = urlsplit(settings["url"])
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or not parsed.port
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or _metadata({"ui_state": settings["metadata"]}) is None
        or not isinstance(settings["directory"], str)
        or not settings["directory"].startswith("/")
        or type(settings["timeout"]) not in (int, float)
        or not math.isfinite(settings["timeout"])
        or not 0 < settings["timeout"] <= 30
    ):
        raise StateError("invalid-binding")
    deadline = time.monotonic() + settings["timeout"]
    password = os.environ["OPENCODE_PASSWORD"]
    authorization = (
        "Basic " + base64.b64encode(("opencode:" + password).encode()).decode()
    )

    def request(method, route, body):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise StateError("transport")
        client = HTTPConnection(parsed.hostname, parsed.port, timeout=remaining)
        try:
            encoded = json.dumps(body).encode() if body is not None else None
            client.request(
                method,
                route,
                encoded,
                {
                    "Authorization": authorization,
                    "Content-Type": "application/json",
                },
            )
            response = client.getresponse()
            content = response.read(MAX_REPLY + 1)
            if response.status >= 400:
                try:
                    failure = json.loads(content)
                    kind = failure.get("type")
                except Exception:
                    kind = None
                if kind in ("rpc.invalid_input", "rpc.invalid_output"):
                    raise StateError(kind.replace(".", "-").replace("_", "-"))
                category = "native-" + str(response.status)
                raise StateError(category if category in REASONS else "transport")
            if len(content) > MAX_REPLY:
                raise StateError("transport")
            return json.loads(content)
        finally:
            client.close()

    try:
        answer = verified_snapshot(settings, request)
    except StateError as error:
        answer = Snapshot("invalid", reason=str(error))
    except Exception:
        answer = Snapshot("missing", reason="transport")
    print(json.dumps(asdict(answer)), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps(asdict(Snapshot("missing", reason="transport"))), flush=True)
