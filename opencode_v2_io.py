"""Private standard-CLI V2 service lease for credential refresh and migration.

Used inside a workspace (and offline fixtures). Secret input/output stays in
memory/pipes; a fresh --stdio server never elects or modifies a shared service.
"""

import base64
import contextlib
import json
import os
from pathlib import Path
import secrets
import select
import signal
import sqlite3
import subprocess
import sys
import time
from urllib.parse import quote, urlparse
from urllib.request import Request, build_opener, ProxyHandler


@contextlib.contextmanager
def server(binary):
    password = secrets.token_urlsafe(32)
    env = {**os.environ, "OPENCODE_PASSWORD": password}
    descriptor = os.memfd_create("oc-box-v2-errors", os.MFD_CLOEXEC)
    with os.fdopen(descriptor, "w+b") as errors:
        child = subprocess.Popen(
            [binary, "serve", "--stdio", "--port", "0"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            env=env,
            start_new_session=True,
        )
        assert child.stdout is not None
        try:
            deadline = time.monotonic() + 45
            output = bytearray()
            while b"\n" not in output:
                if child.poll() is not None or time.monotonic() > deadline:
                    raise ValueError(
                        "Private OpenCode 2.x server did not become ready."
                    )
                if select.select([child.stdout], [], [], 0.1)[0]:
                    output.extend(os.read(child.stdout.fileno(), 4096))
                if len(output) > 65536:
                    raise ValueError("Invalid private OpenCode 2.x readiness response.")
            address = json.loads(output.split(b"\n", 1)[0])["url"]
            parsed = urlparse(address)
            if (
                parsed.scheme != "http"
                or parsed.hostname != "127.0.0.1"
                or not parsed.port
            ):
                raise ValueError("Private OpenCode 2.x server did not bind loopback.")
            auth = (
                "Basic " + base64.b64encode(("opencode:" + password).encode()).decode()
            )
            opener = build_opener(ProxyHandler({}))

            def request(method, path, data=None):
                req = Request(
                    address + path,
                    method=method,
                    data=json.dumps(data).encode() if data is not None else None,
                    headers={"Authorization": auth, "Content-Type": "application/json"},
                )
                with opener.open(req, timeout=60) as response:
                    body = response.read()
                    return json.loads(body) if body else None

            # Migration is asynchronous in upstream. Do not mistake an empty
            # session list for successful completion and lose the resume ID.
            deadline = time.monotonic() + 120
            while True:
                status = request("GET", "/api/experimental/migration/v1")
                if not isinstance(status, dict):
                    raise ValueError("Invalid OpenCode migration response.")
                status = status.get("data", status)
                if status.get("status") == "completed":
                    break
                if status.get("status") == "error" or time.monotonic() > deadline:
                    raise ValueError(
                        "OpenCode's V1-to-V2 session migration failed or timed out; the original V1 database is retained."
                    )
                time.sleep(0.1)
            yield request
        finally:
            if child.stdin:
                child.stdin.close()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
            if child.stdout:
                child.stdout.close()


def seed_database():
    root = Path(os.environ["XDG_DATA_HOME"]) / "opencode"
    source, target = root / "opencode.db", root / "oc-box-v2.db"
    if not source.is_file() or target.exists():
        return
    staged = root / (".oc-box-v2-" + secrets.token_hex(8) + ".db")
    try:
        descriptor = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(descriptor)
        with (
            contextlib.closing(
                sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
            ) as old,
            contextlib.closing(sqlite3.connect(staged)) as new,
        ):
            old.backup(new)
        os.link(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def refresh_credentials(binary, entries):
    if not isinstance(entries, list):
        raise ValueError("Invalid OpenCode credential export.")
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("id"), str)
            or not isinstance(entry.get("value"), dict)
            or not isinstance(entry.get("label"), str)
        ):
            raise ValueError("Invalid OpenCode credential entry.")
    with server(binary) as request:
        current = {
            entry["id"]: entry for entry in request("GET", "/api/credential")["data"]
        }
        for entry in entries:
            old = current.get(entry["id"])
            comparable = ("integrationID", "label", "value")
            if old and any(old.get(key) != entry.get(key) for key in comparable):
                request("DELETE", "/api/credential/" + quote(entry["id"], safe=""))
                old = None
            if old is None:
                request(
                    "POST",
                    "/api/credential",
                    {
                        key: entry[key]
                        for key in ("id", "integrationID", "label", "value")
                        if entry.get(key) is not None
                    }
                    | {"activate": bool(entry.get("active"))},
                )
        # Apply host active selections after all replacements, so the first
        # inactive entry in a new integration cannot accidentally win.
        for entry in entries:
            if entry.get("active"):
                request(
                    "POST",
                    "/api/credential/" + quote(entry["id"], safe="") + "/activate",
                )


def main(argv):
    action, binary = argv
    if action == "seed":
        seed_database()
    elif action == "auth":
        refresh_credentials(binary, json.load(sys.stdin))
    elif action == "ready":
        with server(binary):
            pass
    elif action == "plugins":
        with server(binary) as request:
            query = "?directory=" + quote(os.getcwd(), safe="")
            request("GET", "/api/model/default" + query)
            plugins = request("GET", "/api/plugin" + query)["data"]
            if not any(
                plugin.get("id") == "oc-box.build-permissions"
                and plugin.get("state", {}).get("status") == "active"
                for plugin in plugins
            ):
                raise ValueError(
                    "The oc-box Build permissions plugin did not activate."
                )
            for plugin in plugins:
                if plugin.get("state", {}).get("status") == "failed":
                    source = plugin.get("source", {})
                    name = (
                        source.get("target")
                        or source.get("path")
                        or plugin.get("id")
                        or "unknown"
                    )
                    print(
                        "oc-box: OpenCode 2.x plugin unavailable: "
                        + str(name)
                        + "; port this plugin to the V2 API or install its V2-compatible release."
                    )
                    error = str(plugin.get("state", {}).get("error", ""))
                    if "oc-box V1 adapter: unsupported hooks " in error:
                        import re

                        match = re.search(
                            r"oc-box V1 adapter: unsupported hooks ([A-Za-z0-9., _-]+)",
                            error,
                        )
                        if match:
                            print(
                                "oc-box: OpenCode 2.x plugin unavailable: unsupported legacy hooks: "
                                + match[1][:300]
                            )
    else:
        raise ValueError("Unknown private OpenCode operation.")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception:
        # Native errors can contain credential-bearing request/response data.
        print(
            "Private OpenCode 2.x operation failed; no private tool output was logged.",
            file=sys.stderr,
        )
        sys.exit(1)
