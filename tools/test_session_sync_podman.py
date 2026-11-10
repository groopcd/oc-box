#!/usr/bin/env python3
"""Fresh-only HOST history/readiness acceptance against an immutable checkout.

Run this file OUTSIDE --source with python3 -I -B. The runner is independently
hashed; all application and donor helpers come from the clean, full-history
--source/--expect-commit pair. No resume, automatic cleanup, real credentials,
normal host service, paid inference, or interactive UI acceptance is provided.
Offline tests of this runner do NOT constitute rootless Podman acceptance.
"""

import sys

# Direct entry must be isolated before argparse (or any other shadowable import).
# Imported unit/library use is not direct startup and never bypasses main's guard.
if __name__ == "__main__" and not (sys.flags.isolated and sys.dont_write_bytecode):
    sys.stderr.write("session-history fixture: category=invalid-options\n")
    raise SystemExit(2)

import argparse
import contextlib
import hashlib
import importlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import resource
import secrets
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

FORMAT = "oc-box-session-sync-host-v1"
MARKER = "history-purpose.json"
REPORT = "history-result.json"
DONORS = (
    "tools/acceptance_guards.py",
    "tools/test_global_vault_podman.py",
    "tools/test_v2_podman.py",
    "tools/test_interactive_podman.py",
)
CHECKS = frozenset(
    (
        "private_store",
        "native_version",
        "not_ready_refusal",
        "ready_attach",
        "paginated_inventory",
        "scoped_graph",
        "independent_archives",
        "parent_replacement",
        "deleted_source_retention",
        "idempotent_retry",
        "host_child_refusal",
        "host_child_recovery",
        "idle_pty_readiness",
        "foreground_wait_pending",
        "foreground_recovery",
        "background_running_idle",
        "background_job_wait",
        "background_notice_wakeup",
        "background_final_quiet",
        "held_lease_shutdown",
        "running_shell_filtered",
        "incomplete_export_refusal",
        "pending_fixed_reason",
        "pending_aggregate_reason",
        "pending_session_order",
        "pending_earlier_progress",
        "pending_later_progress",
        "pending_archive_preservation",
        "pending_exit",
        "sync_recovery",
        "owner_cleanup",
        "unchanged_identity",
        "host_clean",
    )
)
EGG_INFO = frozenset(
    (
        b"PKG-INFO",
        b"SOURCES.txt",
        b"dependency_links.txt",
        b"entry_points.txt",
        b"requires.txt",
        b"top_level.txt",
    )
)
CATEGORIES = frozenset(
    (
        "host-only",
        "invalid-options",
        "unsafe-path",
        "placement",
        "fresh-parent-required",
        "clean-source-required",
        "source-changed",
        "source-shadowing",
        "runner-placement",
        "native-changed",
        "fixture-purpose",
        "fixture-only",
        "report-schema",
        "missing-checks",
        "oracle",
        "worker-timeout",
        "worker-failed",
        "gate-timeout",
        "provider-contract",
        "provider-shutdown",
        "unexpected-success",
        "launcher-exit",
        "unexpected-unlock",
        "io-error",
        "subprocess-error",
        "interrupted",
        "operation-failed",
        "fixture-refusal",
    )
)
SCOPE = (
    "Scripted native API/CLI, real rootless launcher/history/readiness. "
    "Not interactive UI, real-provider, network-containment, vault or backup acceptance."
)
STAGES = frozenset(
    (
        "preparation",
        "private-store",
        "native-version",
        "graph",
        "readiness",
        "background",
        "shell",
        "recovery-sync",
        "complete",
    )
)


class Refusal(Exception):
    """Only a fixed category, never native errors, arguments or private values."""


def require(value, category="oracle"):
    if not value:
        raise Refusal(category)


def host_guard():
    # Deliberately stdlib-only: no source imports or probes before this guard.
    require(
        os.geteuid() != 0
        and os.environ.get("OC_BOX_CONTAINER") != "1"
        and not Path("/.dockerenv").exists()
        and not Path("/run/.containerenv").exists(),
        "host-only",
    )


def safe_path(path):
    path = Path(path).expanduser().absolute()
    require(
        ".." not in path.parts and not any(c in str(path) for c in "\0\r\n"),
        "unsafe-path",
    )
    for ancestor in (*reversed(path.parents), path):
        try:
            mode = ancestor.lstat().st_mode
        except FileNotFoundError:
            continue
        require(not stat.S_ISLNK(mode), "unsafe-path")
        if ancestor != path:
            require(stat.S_ISDIR(mode), "unsafe-path")
    return path


def fresh_parent(path):
    parent = safe_path(path)
    require(parent.is_dir(), "placement")
    for ancestor in (parent, *parent.parents):
        require(
            not ancestor.name.startswith(
                ("oc-box-gv-host-", "oc-box-live-v2-", "oc-box-live-backup-")
            )
            and not any(
                (ancestor / name).exists() or (ancestor / name).is_symlink()
                for name in (
                    "fixture.json",
                    "fixture-ids.json",
                    "interactive.json",
                    "edges.json",
                    MARKER,
                )
            ),
            "fresh-parent-required",
        )
    return parent


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(source, *arguments):
    executable = shutil.which("git")
    require(
        executable is not None and Path(executable).is_absolute(),
        "clean-source-required",
    )
    return subprocess.run(
        [executable, "-C", str(source), *arguments],
        env={
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=15,
    ).stdout


def source_snapshot(source, expected):
    source = safe_path(source)
    require(
        source.is_dir() and re.fullmatch(r"[0-9a-f]{40}", expected or ""),
        "clean-source-required",
    )
    require(
        git(source, "rev-parse", "--show-toplevel").decode().strip() == str(source)
        and git(source, "rev-parse", "HEAD").decode().strip() == expected
        and git(source, "rev-parse", "--is-shallow-repository").strip() == b"false"
        and not git(source, "status", "--porcelain", "--untracked-files=all"),
        "clean-source-required",
    )
    count = int(git(source, "rev-list", "--count", "HEAD"))
    require(count > 29, "clean-source-required")
    ignored = git(
        source,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
    ).split(b"\0")
    # Status omits ignored files. Bytecode caches are never read (see
    # isolate_bytecode) and editable-build metadata is data, not code; the import
    # guard additionally requires every loaded module under the source to be a
    # hashed tracked file. The checkout root is also an import path: refuse its
    # ignored Python/package/extension code, not just code below src and tools.
    require(
        all(
            (item.endswith(b".pyc") and b"/__pycache__/" in b"/" + item)
            or (
                item.startswith(b"src/oc_box.egg-info/")
                and item.rsplit(b"/", 1)[-1] in EGG_INFO
            )
            or (
                not item.startswith((b"src/", b"tools/"))
                and not item.endswith((b".py", b".pyc", b".pyo", b".so", b".pyd"))
            )
            for item in ignored
            if item
        ),
        "clean-source-required",
    )
    records = git(
        source,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        expected,
        "--",
        "src/oc_box",
        *DONORS,
    ).split(b"\0")
    hashes = {}
    for record in filter(None, records):
        header, name = record.split(b"\t", 1)
        mode, kind, blob = header.split()
        relative = name.decode()
        require(
            mode in (b"100644", b"100755") and kind == b"blob", "clean-source-required"
        )
        path = safe_path(source / relative)
        content = path.read_bytes()
        require(
            hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content)
            .hexdigest()
            .encode()
            == blob,
            "source-changed",
        )
        hashes[relative] = hashlib.sha256(content).hexdigest()
    require(
        set(DONORS) <= hashes.keys() and "src/oc_box/oc_box.py" in hashes,
        "clean-source-required",
    )
    return {
        "commit": expected,
        "commit_count": count,
        "version": f"1.6.{count - 29}+g{expected[:7]}",
        "dirty": False,
        "sha256": hashes,
    }


def import_guard(source, tracked=None):
    """Never reuse a runtime/helper imported from another checkout or install."""
    for name, module in tuple(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if name in ("oc_box", "tools") or name.startswith(("oc_box.", "tools.")):
            locations = ([filename] if filename else []) + list(
                getattr(module, "__path__", ())
            )
            require(
                locations
                and all(
                    Path(item).resolve().is_relative_to(source) for item in locations
                ),
                "source-shadowing",
            )
        if tracked is not None and filename:
            resolved = Path(filename).resolve()
            if resolved.is_relative_to(source):
                origin = getattr(getattr(module, "__spec__", None), "origin", None)
                require(
                    resolved.relative_to(source).as_posix() in tracked
                    and (origin is None or Path(origin).resolve() == resolved),
                    "source-shadowing",
                )
                require(
                    file_hash(safe_path(filename))
                    == tracked[resolved.relative_to(source).as_posix()],
                    "source-changed",
                )


def isolate_bytecode():
    """Ignore every on-disk bytecode cache: recompile hashed sources only.

    A stale __pycache__ entry with matching mtime/size would otherwise execute
    instead of the hashed .py file, with __file__ still naming that source.
    """
    sys.dont_write_bytecode = True
    sys.pycache_prefix = "/nonexistent-oc-box-bytecode-" + secrets.token_hex(16)


def interactive_bootstrap(source, hashes):
    """Execute only authenticated donor bytes in a pinned, code-free namespace."""
    name = "tools.test_interactive_podman"
    path = safe_path(source / "tools/test_interactive_podman.py")
    content = path.read_bytes()
    require(
        hashlib.sha256(content).hexdigest()
        == hashes["tools/test_interactive_podman.py"],
        "source-changed",
    )
    package = ModuleType("tools")
    package.__path__ = [str(safe_path(source / "tools"))]
    package.__package__ = "tools"
    package.__spec__ = importlib.machinery.ModuleSpec(
        "tools", loader=None, is_package=True
    )
    package.__spec__.submodule_search_locations = package.__path__
    sys.modules["tools"] = package
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    exec(compile(content, str(path), "exec"), module.__dict__)  # noqa: S102 -- authenticated commit bytes, never cached code
    package.test_interactive_podman = module
    return module


def load_source(source, expected):
    identity = source_snapshot(source, expected)
    source = safe_path(source)
    require(not Path(__file__).resolve().is_relative_to(source), "runner-placement")
    import_guard(source)
    # Current file bytes cannot attest prior same-path in-memory implementations.
    require(
        not any(
            name in ("oc_box", "tools") or name.startswith(("oc_box.", "tools."))
            for name in sys.modules
        ),
        "source-shadowing",
    )
    isolate_bytecode()
    # Neither a root tools.py/pyc nor a foreign regular tools package may execute
    # before origin checks. The donor is a verified stdlib bootstrap, not proof
    # of this external runner's own initial trust or outside-source placement.
    interactive = interactive_bootstrap(source, identity["sha256"])
    sys.path[:0] = [str(source / "src"), str(source)]
    interactive.load_source(source, expected)
    guards = importlib.import_module("tools.acceptance_guards")
    fixture = importlib.import_module("tools.test_global_vault_podman")
    import_guard(source, identity["sha256"])
    require(
        fixture.APP.resolve() == source
        and fixture.box.PACKAGE.resolve() == source / "src/oc_box",
        "source-shadowing",
    )
    require(source_snapshot(source, expected) == identity, "source-changed")
    return SimpleNamespace(
        guards=guards,
        fixture=fixture,
        interactive=interactive,
        box=fixture.box,
        service=fixture.workspace_service,
        source=source,
        identity=identity,
    )


def category(error):
    # Donor FixtureError messages are fixed public categories too.
    if isinstance(error, Refusal) or type(error).__name__ == "FixtureError":
        return str(error) if str(error) in CATEGORIES else "fixture-refusal"
    if isinstance(error, OSError):
        return "io-error"
    if isinstance(error, subprocess.SubprocessError):
        return "subprocess-error"
    return (
        "interrupted"
        if isinstance(error, (KeyboardInterrupt, SystemExit))
        else "operation-failed"
    )


FRAME = re.compile(
    r"[A-Za-z0-9_.-]{1,80}\.py:(?:<[a-z]{1,12}>|[A-Za-z_][A-Za-z0-9_]{0,63}):[0-9]{1,6}"
)
TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def trace(error):
    """Code locations only: exception class names and file:function:line frames.

    Never messages, arguments, locals or paths, so private native output cannot
    appear. Follows the explicit cause/context chain (bounded) so a failure that
    crossed a worker or a wrapper still identifies its original location.
    """
    entries, seen = [], set()
    while error is not None and id(error) not in seen and len(entries) < 4:
        seen.add(id(error))
        frames = [
            f"{Path(frame.filename).name}:{frame.name}:{frame.lineno}"
            for frame in traceback.extract_tb(error.__traceback__)
        ][-6:]
        name = type(error).__name__
        entries.append(
            {
                "type": name if TYPE.fullmatch(name) else "other",
                "frames": [item for item in frames if FRAME.fullmatch(item)],
            }
        )
        error = error.__cause__ or error.__context__
    return entries


def result(
    checks,
    identity,
    runner,
    native,
    primary=None,
    secondary=(),
    exits=(),
    *,
    stage="preparation",
    failure_trace=(),
):
    require(stage in STAGES, "report-schema")
    require(
        isinstance(failure_trace, (list, tuple))
        and len(failure_trace) <= 4
        and all(
            isinstance(entry, dict)
            and set(entry) == {"type", "frames"}
            and isinstance(entry["type"], str)
            and TYPE.fullmatch(entry["type"])
            and isinstance(entry["frames"], list)
            and len(entry["frames"]) <= 6
            and all(
                isinstance(item, str) and FRAME.fullmatch(item)
                for item in entry["frames"]
            )
            for entry in failure_trace
        ),
        "report-schema",
    )
    require(
        isinstance(identity, dict)
        and set(identity) == {"commit", "commit_count", "version", "dirty", "sha256"},
        "report-schema",
    )
    require(
        isinstance(identity["commit"], str)
        and re.fullmatch(r"[0-9a-f]{40}", identity["commit"]),
        "report-schema",
    )
    require(
        type(identity["commit_count"]) is int
        and identity["commit_count"] > 29
        and identity["dirty"] is False,
        "report-schema",
    )
    require(
        identity["version"]
        == f"1.6.{identity['commit_count'] - 29}+g{identity['commit'][:7]}",
        "report-schema",
    )
    require(
        isinstance(identity["sha256"], dict)
        and identity["sha256"]
        and all(
            isinstance(name, str)
            and not Path(name).is_absolute()
            and ".." not in Path(name).parts
            and isinstance(digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", digest)
            for name, digest in identity["sha256"].items()
        ),
        "report-schema",
    )
    require(
        isinstance(runner, str) and re.fullmatch(r"[0-9a-f]{64}", runner),
        "report-schema",
    )
    require(
        isinstance(native, dict)
        and set(native) == {"version", "sha256"}
        and native["version"] == "2.2.6"
        and isinstance(native["sha256"], str)
        and re.fullmatch(r"[0-9a-f]{64}", native["sha256"]),
        "report-schema",
    )
    require(
        set(checks) <= CHECKS and all(type(value) is bool for value in checks.values()),
        "report-schema",
    )
    require(primary is None or primary in CATEGORIES, "report-schema")
    require(all(item in CATEGORIES for item in secondary), "report-schema")
    require(
        all(type(code) is int and -255 <= code <= 255 for code in exits),
        "report-schema",
    )
    if primary is None and (set(checks) != CHECKS or not all(checks.values())):
        primary = "missing-checks"
    return {
        "format": FORMAT,
        "stage": stage,
        "status": "PASS" if primary is None and not secondary else "FAIL",
        "source_identity": identity,
        "runner_sha256": runner,
        "native": native,
        "checks": checks,
        "launcher_exit_codes": list(exits),
        "primary_category": primary,
        "secondary_categories": list(secondary),
        "failure_trace": list(failure_trace),
        "scope": SCOPE,
        "not_covered": [
            "transient completed-job row (not required or claimed)",
            "malformed/inconsistent API inventories (offline tests only)",
        ],
        "observers": [
            "scripted run_tui",
            "forwarding Owner.publish/wait_clients",
            "release gate after actual sync refusal",
            "transparent actual readiness API pair counts",
        ],
    }


class Worker:
    """Bounded explicit-connection work; exceptions never escape into reports."""

    def __init__(self, callback):
        self.done = threading.Event()
        self.error = None
        self.value = None

        def run():
            try:
                self.value = callback()
            except BaseException as error:  # noqa: BLE001 -- Private errors remain in memory.
                self.error = error
            finally:
                self.done.set()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def join(self, timeout=20):
        self.thread.join(timeout)
        require(not self.thread.is_alive(), "worker-timeout")
        if self.error is not None:
            raise Refusal("worker-failed") from self.error
        return self.value


def expect_refusal(callback, exception):
    try:
        callback()
    except exception as error:
        return error
    raise Refusal("unexpected-success")


def expect_context_refusal(manager, exception):
    def enter():
        with manager:
            pass

    return expect_refusal(enter, exception)


def preserved(before, after, *, private=True):
    """Host-child refusal may update private JSON, but never its success digest."""
    require(before["host"] == after["host"] and before["synced"] == after["synced"])
    if private:
        require(before["private"] == after["private"])


def finish_actions(actions, secondary):
    """Try every gate/join, preserving an in-flight primary exception."""
    primary = sys.exc_info()[1]
    first = None
    for action in actions:
        try:
            action()
        except BaseException as error:  # noqa: BLE001 -- Preserve primary even on interruption.
            if primary is None and first is None:
                first = error
            else:
                secondary.append(category(error))
    if first is not None:
        raise first


class Provider:
    """A bounded synthetic stream hold, not a replacement OpenCode transport."""

    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()
        self.woken, self.wake_release = threading.Event(), threading.Event()
        self.background = None
        self.background_requests = 0
        self.requests = self.errors = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                # Production's optional Lemonade discovery must not touch an
                # actual host model service. Route it to this bounded empty
                # synthetic catalog via its supported nonsecret setting.
                accepted = self.path in (
                    "/api/v1/models",
                    "/api/v1/models/",
                    "/api/v1/health",
                    "/api/v1/version",
                )
                content = b'{"data":[],"models":[]}'
                self.send_response(200 if accepted else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def do_POST(self):
                try:
                    self.connection.settimeout(15)
                    size = int(self.headers.get("Content-Length", "0"))
                    require(
                        self.path == "/v1/chat/completions" and 0 < size < 2_000_000,
                        "provider-contract",
                    )
                    body = json.loads(self.rfile.read(size))
                    require(
                        body.get("stream") is True
                        and body.get("model") == "gpt-history",
                        "provider-contract",
                    )
                    outer.requests += 1
                    delta, finish = outer.reply(body)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.flush()
                    for part, end in ((delta, None), ({}, finish)):
                        payload = {
                            "id": "chatcmpl-history",
                            "object": "chat.completion.chunk",
                            "model": "gpt-history",
                            "choices": [
                                {"index": 0, "delta": part, "finish_reason": end}
                            ],
                            "usage": {
                                "prompt_tokens": 1,
                                "completion_tokens": 1,
                                "total_tokens": 2,
                            },
                        }
                        self.wfile.write(
                            ("data: " + json.dumps(payload) + "\n\n").encode()
                        )
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except BaseException:  # noqa: BLE001 -- Never log native provider payloads.
                    outer.errors += 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.release.set()
        self.wake_release.set()
        closer = Worker(self.server.shutdown)
        try:
            closer.join(3)
        finally:
            self.server.server_close()
            self.thread.join(3)
        require(not self.thread.is_alive(), "provider-shutdown")

    def reply(self, body):
        if self.background is None:
            require(self.requests == 1, "provider-contract")
            self.entered.set()
            require(self.release.wait(90), "gate-timeout")
            return {"content": "SYNTHETIC_HISTORY_FOREGROUND_DONE"}, "stop"
        self.background_requests += 1
        phase = self.background_requests
        require(phase <= 3, "provider-contract")
        messages = body.get("messages", [])
        if phase == 1:
            functions = {
                item["function"]["name"]: item["function"]
                for item in body.get("tools", [])
            }
            require("shell" in functions, "provider-contract")
            schema = functions["shell"]["parameters"].get("properties", {})
            require(
                schema.get("background", {}).get("type") == "boolean",
                "provider-contract",
            )
            require(
                schema.get("command", {}).get("type") == "string"
                and schema.get("workdir", {}).get("type") == "string",
                "provider-contract",
            )
            return {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_history_background",
                        "type": "function",
                        "function": {
                            "name": "shell",
                            "arguments": json.dumps(self.background),
                        },
                    }
                ]
            }, "tool_calls"
        require(
            any(item.get("role") == "tool" for item in messages), "provider-contract"
        )
        if phase == 2:
            return {"content": "FOREGROUND_IDLE_MARKER"}, "stop"
        text = json.dumps([item for item in messages if item.get("role") == "user"])
        require(
            'state=\\"completed\\"' in text and "SYNTHETIC_HISTORY_BG_DONE" in text,
            "provider-contract",
        )
        self.woken.set()
        # BEFORE headers: native foreground execution is held after the notice
        # has removed the background job. Never require a transient completed row.
        require(self.wake_release.wait(90), "gate-timeout")
        return {"content": "REAL_WAKE_ACK_MARKER"}, "stop"

    def config(self):
        return {
            "$schema": "https://opencode.ai/config.json",
            "snapshots": False,
            "formatter": False,
            "update": "disable",
            "default_agent": "build",
            "policies": [
                {"action": "provider.use", "resource": "*", "effect": "deny"},
                {"action": "provider.use", "resource": "fixture", "effect": "allow"},
            ],
            "providers": {
                "fixture": {
                    "package": "aisdk:@ai-sdk/openai-compatible",
                    "settings": {
                        "baseURL": f"http://host.containers.internal:{self.server.server_port}/v1",
                        "apiKey": "synthetic-not-a-credential",
                    },
                    "models": {
                        "gpt-history": {
                            "name": "Synthetic history fixture",
                            "limit": {"context": 32000, "output": 2000},
                        }
                    },
                }
            },
            "model": "fixture/gpt-history",
        }


class PairObserver:
    """Forward real CLI requests unchanged; retain only boolean pair counts."""

    def __init__(self, original, session):
        self.original, self.session = original, session
        self.thread = None
        self.phase = "background"
        self.condition = threading.Condition()
        self.counts = {"foreground": 0, "background": 0, "wake": 0, "quiet": 0}
        self.active = None

    def __call__(self, box, data, connection, method, target, value=None, **options):
        response = self.original(
            box, data, connection, method, target, value, **options
        )
        if threading.get_ident() == self.thread and method == "GET":
            with self.condition:
                if target == "/api/session/active":
                    active = (
                        response.get("data") if isinstance(response, dict) else None
                    )
                    self.active = (
                        active == {},
                        isinstance(active, dict) and self.session in active,
                    )
                elif target == "/api/experimental/job" and self.active is not None:
                    jobs = response.get("data") if isinstance(response, dict) else None
                    idle, running = self.active
                    if self.phase == "foreground" and running:
                        self.counts["foreground"] += 1
                    if (
                        self.phase == "background"
                        and idle
                        and isinstance(jobs, list)
                        and any(
                            item.get("sessionID") == self.session
                            and item.get("kind") == "shell"
                            and item.get("status") == "running"
                            for item in jobs
                        )
                    ):
                        self.counts["background"] += 1
                    if self.phase == "wake" and running and jobs == []:
                        self.counts["wake"] += 1
                    if self.phase == "quiet" and idle and jobs == []:
                        self.counts["quiet"] += 1
                    self.active = None
                    self.condition.notify_all()
        return response

    def wait(self, phase, count, worker, *, timeout=60):
        deadline = time.monotonic() + timeout
        with self.condition:
            while self.counts[phase] < count:
                require(
                    not worker.done.is_set() and time.monotonic() < deadline,
                    "worker-timeout",
                )
                self.condition.wait(min(0.1, max(0, deadline - time.monotonic())))
        require(not worker.done.is_set(), "oracle")


class Harness:
    def __init__(self, loaded, root, binary, runner):
        self.loaded, self.f = loaded, loaded.fixture
        self.box, self.service = loaded.box, loaded.service
        self.root, self.binary, self.runner = root, binary, runner
        try:
            self.native_identity = loaded.guards.native_identity(
                binary, outside=(loaded.source,)
            )
        except loaded.guards.GuardError as error:
            raise Refusal("fixture-refusal") from error
        self.native = {
            "version": self.f.NATIVE_VERSION,
            "sha256": self.native_identity.sha256,
        }
        self.checks, self.exits, self.secondary = {}, [], []
        self.stage = "preparation"
        self.progress = lambda _name: None
        self.provider = None
        self.path = self.data = self.connection = None

    def check(self, name):
        require(name in CHECKS, "report-schema")
        if name not in self.checks:
            self.progress(name)
        self.checks[name] = True

    def same_identity(self):
        require(
            source_snapshot(self.loaded.source, self.loaded.identity["commit"])
            == self.loaded.identity,
            "source-changed",
        )
        import_guard(self.loaded.source, self.loaded.identity["sha256"])
        require(file_hash(__file__) == self.runner, "source-changed")
        try:
            self.loaded.guards.check_native(self.native_identity)
        except self.loaded.guards.GuardError as error:
            raise Refusal("native-changed") from error

    def purpose(self):
        return {
            "format": FORMAT,
            "source_identity": self.loaded.identity,
            "runner_sha256": self.runner,
            "native": self.native,
            "projects": ["source-a"],
        }

    def validate(self, *, initial=False):
        self.same_identity()
        self.f.validate_fixture(self.root, allow_unrecorded=True)
        require(
            self.f.private_json(self.root, MARKER) == self.purpose(), "fixture-purpose"
        )
        records = self.f.workspace_records(self.root)
        require(set(records) <= {"source-a"}, "fixture-only")
        require(
            all("credentials" not in data for _, data in records.values()),
            "fixture-only",
        )
        self.validate_native_config(initial=initial)
        self.f.require_runtime_boot(
            self.root, allow_uninitialized=initial and not records
        )
        self.f.validate_store(self.root, allow_unrecorded=True)
        return records

    def validate_native_config(self, *, initial=False):
        require(
            self.f.private_json(self.root, "config/opencode/opencode.json")
            == self.provider.config(),
            "fixture-purpose",
        )
        require(
            self.f.private_json(self.root, "config/oc-box/config.json")
            == {
                "default_distro": self.distro,
                "pass_env": [self.f.PASS_ENV],
                "auto_update": False,
            },
            "fixture-purpose",
        )
        require(not self.f.vault.vault_path().exists(), "fixture-only")
        if initial:
            require(not (self.root / "executables.json").exists(), "fixture-only")
        else:
            require(
                self.f.private_json(self.root, "executables.json")
                == {"2": str(self.binary)},
                "native-changed",
            )
            try:
                wrapper = self.loaded.guards.checked_path(
                    "bin/opencode", root=self.root, kind="file"
                )
                self.loaded.guards.exact_private_files(
                    self.root,
                    {"bin/opencode": self.f.native_wrapper_source().encode()},
                    uid=os.getuid(),
                )
            except self.loaded.guards.GuardError as error:
                raise Refusal("native-changed") from error
            require(
                os.access(wrapper, os.X_OK),
                "native-changed",
            )

    def bind(self, command):
        self.f.record_ids(self.root)
        self.path, self.data = self.f.workspace(self.root, "source-a")
        self.validate()
        self.f.verify_owned_resources(self.root, self.data)
        self.connection = self.service.live_connection()
        require(self.connection is not None, "oracle")
        return self.f.selected_session(command, self.path)

    def container_python(self, code, payload=None, *, timeout=60):
        command = self.service.client_command(
            self.box, self.data, self.connection, ["python3", "-c", code]
        )
        return self.box.run(
            command,
            input=json.dumps(payload),
            capture=True,
            private=True,
            timeout=timeout,
        ).stdout

    def batch(self, operations):
        # Reuse the production authenticated HTTP bridge inside the actual
        # container. Payload/transcripts/control credentials travel via stdin,
        # never harness argv, project files, or a fake process boundary.
        script = (
            "import json,sys\nnamespace={'__name__':'fixture_bridge'}\nexec("
            + repr((self.box.PACKAGE / "opencode_v2_io.py").read_text())
            + ",namespace)\nsettings=json.load(sys.stdin)\nrequest=namespace['endpoint_request'](settings['endpoint'])\nprint(json.dumps([request(*operation) for operation in settings['operations']]))\n"
        )
        output = self.container_python(
            script,
            {
                "endpoint": {key: self.connection[key] for key in ("url", "password")},
                "operations": operations,
            },
            timeout=180,
        )
        return json.loads(output)

    def request(self, method, route, value=None):
        return self.batch([[method, route, value]])[0]

    def host(self, method, route, value=None):
        if value is not None:
            # Only synthetic seed input uses a private native HTTP lease, as in
            # the donor's seed_native_history. Avoid putting transcript JSON in
            # CLI --data argv. Production sync/import/export transport is never
            # changed, and no normal host service registration is consulted.
            previous = Path.cwd()
            try:
                os.chdir(self.path.parent / "controller")
                with (
                    patch.dict(
                        os.environ, self.f.native_env(self.root, "2"), clear=True
                    ),
                    self.f.bridge.server(str(self.binary)) as request,
                ):
                    return request(method, route, value)
            finally:
                os.chdir(previous)
        # Native host reads/deletes use the production standalone CLI selector.
        args = self.box.host_command(self.data, "api", method.lower(), route)
        output = self.box.run(
            args,
            stdin=subprocess.DEVNULL,
            env=self.box.private_host_env(self.path, self.data),
            cwd=self.path.parent / "controller",
            capture=True,
            private=True,
            timeout=60,
        ).stdout
        return json.loads(output) if output.strip() else None

    def seed_value(self, ident, directory, *, parent=None, archived=False):
        info = {
            "id": ident,
            "projectID": "global",
            "title": ident,
            "cost": 0,
            "tokens": {
                "input": 0,
                "output": 0,
                "reasoning": 0,
                "cache": {"read": 0, "write": 0},
            },
            "time": {"created": 1, "updated": 2},
            "location": {"directory": str(directory)},
            "metadata": {"fixture": "history-host", "opaque": {"id": ident}},
            "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
        }
        if parent:
            info["parentID"] = parent
        if archived:
            info["time"]["archived"] = 1
        return {
            "info": info,
            "messages": [
                {
                    "id": "msg_" + ident.split("_", 1)[-1],
                    "type": "user",
                    "text": "SYNTHETIC_" + ident,
                    "metadata": {"id": ident},
                    "time": {"created": 1},
                }
            ],
            "location": {"directory": str(directory)},
        }

    def seed(self, ident, *, directory=None, parent=None, archived=False, host=False):
        value = self.seed_value(
            ident,
            directory or self.box.container_project(self.data),
            parent=parent,
            archived=archived,
        )
        request = self.host if host else self.request
        request("POST", "/api/experimental/session/import", value)
        return ident

    def export(self, ident, *, host=False):
        return (self.host if host else self.request)(
            "GET", "/api/experimental/session/" + ident + "/export"
        )["data"]

    def archive_id(self, ident, prefix="ses"):
        return self.box.archive_id(self.data["id"] + "-v2", ident, prefix)

    def archive_path(self, ident):
        return self.path.parent / "history" / (self.archive_id(ident) + ".json")

    def snapshot(self, ident):
        return {
            "private": self.archive_path(ident).read_bytes(),
            "host": self.export(self.archive_id(ident), host=True),
            "synced": self.box.session_state(self.path)["synced"][ident],
        }

    def verify_archive(self, ident):
        source = self.export(ident)
        expected = json.loads(json.dumps(source["messages"]))
        for message in expected:
            message["id"] = self.archive_id(message["id"], "msg")
        relationships = {}
        if "parentID" in source["info"]:
            relationships["parentID"] = self.archive_id(source["info"]["parentID"])
        if "fork" in source["info"]:
            fork = source["info"]["fork"]
            relationships["fork"] = {
                "sessionID": self.archive_id(fork["sessionID"]),
                "boundary": {
                    "type": fork["boundary"]["type"],
                    "messageID": self.archive_id(fork["boundary"]["messageID"], "msg"),
                },
            }
        if relationships:
            relationships = {"version": 1, **relationships}
        private = self.box.read_json(self.archive_path(ident))
        for value in (private, self.export(self.archive_id(ident), host=True)):
            require(
                value["messages"] == expected
                and value["info"]["id"] == self.archive_id(ident)
            )
            require(
                not any(
                    key in value["info"]
                    for key in ("parentID", "fork", "permissions", "revert")
                )
            )
            metadata = value["info"].get("metadata", {})
            require(metadata.get("oc-box.archive", {}) == relationships)
            require(
                all(
                    metadata.get(key) == item
                    for key, item in source["info"].get("metadata", {}).items()
                )
            )
            # The launcher-created session has no title; production labels it
            # "Session" (see archive_session), so mirror that default.
            require(
                value["info"]["title"]
                == "[oc-box source-a] " + source["info"].get("title", "Session")
            )
            require(
                value["info"]["time"].get("archived")
                == source["info"]["time"].get("archived")
            )
        self.box.verify_host_archive(self.path, self.data, private)

    def sync(self):
        # Always called on the owning thread: CONNECTION must remain installed.
        require(self.service.live_connection() == self.connection, "oracle")
        self.box.sync_sessions(self.path, self.data)

    def stopped(self):
        self.validate()
        self.f.verify_owned_resources(self.root, self.data, stopped=True)
        require(not self.service.socket_path(self.box, self.data).exists())
        require(self.service.live_connection() is None)
        if hasattr(self, "owner"):
            # Owner releases both references only after its final signal/reap;
            # do not retain or poll the private Popen merely to observe cleanup.
            require(self.owner.child is None and self.owner.child_owner is None)
            require(
                not self.owner.thread.is_alive() and self.owner.listener.fileno() == -1
            )
        with self.box.workspace_lock(self.path):
            pass
        self.f.assert_host_clean()
        require(not self.f.vault.vault_path().exists())

    def launch(self, callback, *, expected=0, observe_sync=None):
        self.stage = callback.__name__
        require(self.stage in STAGES, "report-schema")
        self.validate()
        self.connection = None
        original_publish = self.service.Owner.publish
        original_wait = self.service.Owner.wait_clients
        self.wait_entered = threading.Event()

        def publish(owner, ident):
            self.owner = owner
            path, data = self.f.workspace(self.root, "source-a")
            error = expect_context_refusal(
                self.service.attach(self.box, path, data),
                self.box.BoxError,
            )
            require("not ready" in str(error).lower())
            self.check("not_ready_refusal")
            return original_publish(owner, ident)

        def wait(owner):
            self.wait_entered.set()
            return original_wait(owner)

        def client(command):
            ident = self.bind(command)
            with self.service.attach(self.box, self.path, self.data) as attached:
                require(attached["session"] == ident)
                self.service.api(self.box, self.data, attached, "GET", "/api/info")
            self.check("ready_attach")
            return callback(ident)

        with contextlib.ExitStack() as stack:
            stack.enter_context(self.f.no_vault_probe())
            stack.enter_context(patch.object(self.service.Owner, "publish", publish))
            stack.enter_context(patch.object(self.service.Owner, "wait_clients", wait))
            stack.enter_context(patch.object(self.box, "run_tui", client))
            if observe_sync is not None:
                stack.enter_context(
                    patch.object(self.box, "sync_sessions", observe_sync)
                )
            output, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = self.box.main(
                    ["start", "--project", str(self.root / "source-a"), "--no-vault"]
                )
            if expected:
                require(
                    [
                        line
                        for line in errors.getvalue().splitlines()
                        if line.startswith("oc-box: history sync pending:")
                    ]
                    == ["oc-box: history sync pending: " + self.pending_message()]
                )
                require(
                    "Session history synced to your normal host"
                    not in output.getvalue()
                )
        self.exits.append(code)
        require(code == expected, "launcher-exit")
        self.stopped()

    def refuse_linked_children(self):
        error = expect_refusal(self.sync, self.box.BoxError)
        require("linked child sessions" in str(error))

    def graph(self, selected):
        project = self.box.container_project(self.data)
        self.container_python(
            "import json,pathlib,sys\np=json.load(sys.stdin);pathlib.Path(p['project'],'nested').mkdir();pathlib.Path(p['foreign']).mkdir(parents=True,exist_ok=True)\n",
            {"project": project, "foreign": str(self.root / "source-b")},
        )
        a = self.seed("ses_history_a")
        b = self.seed("ses_history_b")
        c = self.seed("ses_history_c", parent=a)
        d = self.seed("ses_history_d", parent=c)
        archived = self.seed("ses_history_archived", archived=True)
        archived_child = self.seed(
            "ses_history_archived_child", parent=archived, archived=True
        )
        fork = self.request("POST", "/api/session/" + a + "/fork", {})["data"]["id"]
        self.request(
            "POST",
            "/api/session/" + a + "/revert/stage",
            {"messageID": "msg_history_a", "files": False},
        )
        excluded = {
            self.seed("ses_history_nested", directory=str(Path(project) / "nested")),
            self.seed("ses_history_foreign", directory=self.root / "source-b"),
        }
        graph = {selected, a, b, c, d, fork, archived, archived_child}
        padding = ["ses_history_padding_" + str(index).zfill(3) for index in range(101)]
        self.batch(
            [
                [
                    "POST",
                    "/api/experimental/session/import",
                    self.seed_value(ident, project),
                ]
                for ident in padding
            ]
        )
        from urllib.parse import quote

        page = self.request(
            "GET",
            "/api/session?directory="
            + quote(project, safe="")
            + "&order=asc&limit=100",
        )
        require(
            len(page["data"]) == 100 and isinstance(page["cursor"].get("next"), str)
        )
        require(
            {item["id"] for item in self.box.list_container_sessions(self.data)}
            == graph | set(padding)
        )
        self.batch([["DELETE", "/api/session/" + ident, None] for ident in padding])
        self.check("paginated_inventory")
        self.request(
            "PATCH",
            "/api/session/" + d,
            {"title": "Newest child is not the selected root"},
        )
        require(
            {item["id"] for item in self.box.list_container_sessions(self.data)}
            == graph
        )
        parent = self.export(a)
        require(set(parent) == {"info", "messages"} and len(parent["messages"]) == 1)
        require("permissions" in parent["info"] and "revert" in parent["info"])
        require(self.export(archived)["info"]["time"].get("archived") == 1)
        self.sync()
        state = self.box.session_state(self.path)
        require(
            set(state["sessions"]) == graph
            and state["last"] in graph - {c, d, archived_child}
        )
        require(not excluded & set(state["host_sessions"]))
        require(
            {item.stem for item in (self.path.parent / "history").glob("*.json")}
            == {self.archive_id(ident) for ident in graph}
        )
        require(
            {item["id"] for item in self.host("GET", "/api/session?limit=100")["data"]}
            == {self.archive_id(ident) for ident in graph}
        )
        for ident in graph:
            self.verify_archive(ident)
        require(
            self.export(c)["info"]["parentID"] == a
            and self.export(d)["info"]["parentID"] == c
        )
        self.check("scoped_graph")
        self.check("independent_archives")
        children = {ident: self.snapshot(ident) for ident in (c, d)}
        self.request(
            "PATCH", "/api/session/" + a, {"title": "Parent-only source update"}
        )
        self.sync()
        self.verify_archive(a)
        for ident, before in children.items():
            preserved(before, self.snapshot(ident))
        self.check("parent_replacement")
        retained = {ident: self.snapshot(ident) for ident in (a, c, d)}
        self.request("DELETE", "/api/session/" + a)
        self.request(
            "PATCH", "/api/session/" + b, {"title": "Independent root advances"}
        )
        e = self.seed("ses_history_e")
        graph = graph - {a, c, d} | {e}
        self.sync()
        state = self.box.session_state(self.path)
        require(
            set(state["sessions"]) == graph
            and {a, c, d} <= set(state["missing"])
            and state["last"] in graph - {archived_child}
        )
        for ident, before in retained.items():
            preserved(before, self.snapshot(ident))
        self.check("deleted_source_retention")
        before_state = self.box.session_file(self.path).read_bytes()
        before_host = {ident: self.snapshot(ident) for ident in graph}
        self.sync()
        require(self.box.session_file(self.path).read_bytes() == before_state)
        for ident, before in before_host.items():
            preserved(before, self.snapshot(ident))
        self.check("idempotent_retry")
        child = self.seed(
            "ses_history_host_child",
            directory=self.path.parent / "controller",
            parent=self.archive_id(b),
            host=True,
        )
        protected = self.export(child, host=True)
        before = self.snapshot(b)
        self.request(
            "PATCH",
            "/api/session/" + b,
            {"title": "Source advances beneath protected host child"},
        )
        self.refuse_linked_children()
        preserved(before, self.snapshot(b), private=False)
        require(self.export(child, host=True) == protected)
        require(
            self.host(
                "GET", "/api/session?parentID=" + self.archive_id(b) + "&limit=1"
            )["data"]
        )
        self.check("host_child_refusal")
        self.host("DELETE", "/api/session/" + child)
        self.sync()
        self.verify_archive(b)
        self.check("host_child_recovery")
        self.shell_id = self.seed("ses_history_shell")
        self.sync()
        return 0

    def readiness(self, selected):
        from oc_box import session_terminal

        terminal = session_terminal.choose_terminal(
            self.request, selected, self.box.container_project(self.data)
        )
        require(terminal["status"] == "running")
        self.service.wait_history(self.box, self.data, self.connection, timeout=15)
        self.check("idle_pty_readiness")
        child = self.seed("ses_history_foreground", parent=selected)
        worker = Worker(lambda: self.native_run(child, "SYNTHETIC_HISTORY_FOREGROUND"))
        observer = PairObserver(self.service.api, child)
        observer.phase = "foreground"
        waiter = None
        try:
            require(self.provider.entered.wait(30), "gate-timeout")
            require(
                child
                in self.service.api(
                    self.box, self.data, self.connection, "GET", "/api/session/active"
                )["data"]
            )

            def wait():
                observer.thread = threading.get_ident()
                return self.service.wait_history(
                    self.box, self.data, self.connection, timeout=90, interval=0.1
                )

            # One real waiter must remain pending across several ACTUAL pairs
            # showing the child foreground-active, never merely hit a short
            # deadline. Only then is the provider response released.
            with patch.object(self.service, "api", observer):
                waiter = Worker(wait)
                observer.wait("foreground", 4, waiter)
                self.check("foreground_wait_pending")
                self.provider.release.set()
                waiter.join(60)
        finally:
            finish_actions(
                (
                    self.provider.release.set,
                    lambda: require(worker.join(30) == 0),
                    lambda: waiter.join(20) if waiter else None,
                ),
                self.secondary,
            )
        require("SYNTHETIC_HISTORY_FOREGROUND_DONE" in json.dumps(self.export(child)))
        require(
            self.provider.requests == 1 and self.provider.errors == 0,
            "provider-contract",
        )
        self.check("foreground_recovery")
        self.sync()
        before = self.box.session_file(self.path).read_bytes()
        entered = threading.Event()

        def attached():
            with self.service.attach(self.box, self.path, self.data):
                entered.set()
                require(self.wait_entered.wait(15), "gate-timeout")
                info = self.f.verify_owned_resources(self.root, self.data)
                require(info["State"]["Running"] is True)
                require(
                    self.owner.clients >= 1
                    and not self.owner.closing
                    and self.owner.child_owner is not None
                    and self.owner.child_owner.status() is None
                )
                expect_context_refusal(
                    self.box.workspace_lock(self.path),
                    self.box.BoxError,
                )
                require(self.box.session_file(self.path).read_bytes() == before)
                self.check("held_lease_shutdown")

        self.lease = Worker(attached)
        require(entered.wait(10), "gate-timeout")
        return 0

    def native_run(self, ident, prompt):
        code = "import json,subprocess,sys\ns=json.load(sys.stdin)\na=[s['binary'],'run','--server',s['url'],'--session',s['session'],'--model','fixture/gpt-history','--agent','build']\np=subprocess.run(a,input=s['prompt'],text=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=120)\nprint(json.dumps({'exit':p.returncode}))\n"
        return json.loads(
            self.container_python(
                code,
                {
                    "binary": self.box.CONTAINER_OPENCODE,
                    "url": self.connection["url"],
                    "session": ident,
                    "prompt": prompt,
                },
                timeout=150,
            )
        )["exit"]

    def until(self, predicate, *, timeout=30):
        deadline = time.monotonic() + timeout
        while not predicate():
            require(self.provider.errors == 0, "provider-contract")
            require(time.monotonic() < deadline, "gate-timeout")
            time.sleep(0.05)

    def background(self, selected):
        ident = self.seed("ses_history_background", parent=selected)
        gate = ShellGate(self, marker="SYNTHETIC_HISTORY_BG_DONE")
        self.provider.background = {
            "command": gate.command(),
            "workdir": self.box.container_project(self.data),
            "background": True,
        }
        client = Worker(lambda: self.native_run(ident, "SYNTHETIC_HISTORY_BACKGROUND"))
        waiter = None
        observer = PairObserver(self.service.api, ident)

        def inventory():
            active = self.service.api(
                self.box, self.data, self.connection, "GET", "/api/session/active"
            )["data"]
            jobs = self.service.api(
                self.box, self.data, self.connection, "GET", "/api/experimental/job"
            )["data"]
            return active, jobs

        try:

            def running_idle():
                active, jobs = inventory()
                return (
                    active == {}
                    and len(jobs) == 1
                    and jobs[0].get("sessionID") == ident
                    and jobs[0].get("kind") == "shell"
                    and jobs[0].get("status") == "running"
                )

            self.until(running_idle)
            # Native run remains attached while background jobs exist, even
            # though its foreground drain is idle. Join only after the wakeup.
            _, jobs = inventory()
            shell_id, job_id = jobs[0]["shellID"], jobs[0]["id"]
            require("FOREGROUND_IDLE_MARKER" in json.dumps(self.export(ident)))
            self.check("background_running_idle")

            def wait():
                observer.thread = threading.get_ident()
                # ONE real waiter spans native job completion and foreground wake.
                return self.service.wait_history(
                    self.box, self.data, self.connection, timeout=90, interval=0.1
                )

            with patch.object(self.service, "api", observer):
                waiter = Worker(wait)
                observer.wait("background", 4, waiter)
                self.check("background_job_wait")
                gate.release()
                require(self.provider.woken.wait(30), "gate-timeout")

                def woke():
                    active, jobs = inventory()
                    return ident in active and jobs == []

                self.until(woke)
                with observer.condition:
                    observer.phase = "wake"
                observer.wait("wake", 4, waiter)
                messages = self.request(
                    "GET", "/api/session/" + ident + "/message?order=asc&limit=100"
                )["data"]
                notices = [
                    message
                    for message in messages
                    if message.get("type") == "synthetic"
                    and message.get("metadata", {}).get("source") == "shell"
                ]
                require(len(notices) == 1)
                metadata = notices[0]["metadata"]
                require(
                    metadata.get("state") == "completed"
                    and metadata.get("exit") == 0
                    and metadata.get("shellID") == shell_id
                    and metadata.get("jobID") == job_id
                )
                require("SYNTHETIC_HISTORY_BG_DONE" in notices[0].get("text", ""))
                self.check("background_notice_wakeup")
                with observer.condition:
                    observer.phase = "quiet"
                self.provider.wake_release.set()
                waiter.join(30)
                require(client.join(20) == 0)
                require(observer.counts["quiet"] >= 2)
                require(inventory() == ({}, []))
                self.check("background_final_quiet")
            require(
                self.provider.background_requests == 3 and self.provider.errors == 0,
                "provider-contract",
            )
            exported = self.export(ident)
            require(
                notices[0] in exported["messages"]
                and "REAL_WAKE_ACK_MARKER" in json.dumps(exported)
            )
            self.sync()
            self.verify_archive(ident)
            return 0
        finally:
            finish_actions(
                (
                    gate.release,
                    self.provider.wake_release.set,
                    lambda: client.join(20),
                    lambda: waiter.join(20) if waiter else None,
                ),
                self.secondary,
            )

    def shell(self, _selected):
        require(
            self.box.export_container_session(self.data, "ses_history_background")[
                "info"
            ]["id"]
            == "ses_history_background"
        )
        self.gate = ShellGate(self)
        self.shell_before = self.snapshot(self.shell_id)
        self.shell_worker = Worker(
            lambda: self.request(
                "POST",
                "/api/session/" + self.shell_id + "/shell",
                {"id": "msg_history_shell_running", "command": self.gate.command()},
            )
        )
        deadline = time.monotonic() + 20
        while True:
            messages = self.request(
                "GET", "/api/session/" + self.shell_id + "/message?order=asc&limit=100"
            )["data"]
            if any(
                message.get("type") == "shell" and message.get("status") == "running"
                for message in messages
            ):
                break
            require(
                not self.shell_worker.done.is_set() and time.monotonic() < deadline,
                "gate-timeout",
            )
            self.shell_worker.done.wait(0.05)
        require(
            self.shell_id
            not in self.service.api(
                self.box, self.data, self.connection, "GET", "/api/session/active"
            )["data"]
        )
        require(
            not any(
                message.get("type") == "shell"
                for message in self.export(self.shell_id)["messages"]
            )
        )
        self.check("running_shell_filtered")
        self.refuse_incomplete_export()
        state = self.box.session_state(self.path)
        self.pending_earlier = state["sessions"][0]
        require(
            state["sessions"].index(self.pending_earlier)
            < state["sessions"].index(self.shell_id)
        )
        self.pending_earlier_checkpoint = state["synced"][self.pending_earlier]
        require(
            self.pending_earlier_checkpoint
            == file_hash(self.archive_path(self.pending_earlier))
        )
        self.request(
            "PATCH",
            "/api/session/" + self.pending_earlier,
            {"title": "SYNTHETIC_PROGRESS_BEFORE_PENDING"},
        )
        self.pending_later = "ses_history_after_pending"
        require(
            all(
                self.pending_later not in state.get(key, {})
                for key in ("sessions", "synced", "host_sessions")
            )
            and not self.archive_path(self.pending_later).exists()
        )
        self.seed(self.pending_later)
        # Do not sync here: the owner's real failing exit pass must both update
        # the earlier archive and discover/import the new later session.
        return 0

    def refuse_incomplete_export(self):
        error = expect_refusal(
            lambda: self.box.export_container_session(self.data, self.shell_id),
            self.box.HistoryPending,
        )
        require(str(error) == self.box.opencode_v2_io.UNSETTLED)
        preserved(self.shell_before, self.snapshot(self.shell_id))
        self.check("incomplete_export_refusal")
        self.check("pending_fixed_reason")

    def pending_message(self):
        return (
            "1 session(s) not settled yet: "
            + self.box.opencode_v2_io.UNSETTLED
            + " Their last archives are retained and other sessions were synchronized."
        )

    def verify_pending_progress(self, attempted):
        state = self.box.session_state(self.path)
        require(
            state["sessions"].index(self.pending_earlier)
            < state["sessions"].index(self.shell_id)
            < state["sessions"].index(self.pending_later)
        )
        require(
            attempted.index(self.pending_earlier)
            < attempted.index(self.shell_id)
            < attempted.index(self.pending_later)
        )
        self.check("pending_session_order")
        for ident, name in (
            (self.pending_earlier, "pending_earlier_progress"),
            (self.pending_later, "pending_later_progress"),
        ):
            checkpoint = state.get("synced", {}).get(ident)
            require(checkpoint is not None)
            if ident == self.pending_earlier:
                require(checkpoint != self.pending_earlier_checkpoint)
            require(checkpoint == file_hash(self.archive_path(ident)))
            require(state.get("host_sessions", {}).get(ident) == self.archive_id(ident))
            self.verify_archive(ident)
            self.check(name)
        preserved(self.shell_before, self.snapshot(self.shell_id))
        self.check("pending_archive_preservation")

    def pending_observer(self, original):
        def observe(path, data):
            attempted, failed, real_export = [], [], self.box.export_container_session

            def export(data_, ident):
                attempted.append(ident)
                try:
                    return real_export(data_, ident)
                except BaseException as error:
                    failed.append(
                        (
                            ident,
                            isinstance(error, self.box.HistoryPending)
                            and str(error) == self.box.opencode_v2_io.UNSETTLED,
                        )
                    )
                    raise

            try:
                with patch.object(self.box, "export_container_session", export):
                    return original(path, data)
            except self.box.BoxError as error:
                require(failed == [(self.shell_id, True)])
                require(str(error) == self.pending_message())
                self.check("pending_aggregate_reason")
                # Never manufacture or replace a failure. Release only AFTER
                # production rejects the actual incomplete export; preserve its
                # exception identity while allowing native completion to persist.
                self.verify_pending_progress(attempted)
                self.gate.release()
                self.shell_worker.join(25)
                completed = self.export(self.shell_id)
                require(
                    any(
                        message.get("type") == "shell"
                        and message.get("status") == "exited"
                        and message.get("exit") == 0
                        and "SYNTHETIC_HISTORY_SHELL_DONE"
                        in message.get("output", {}).get("output", "")
                        for message in completed["messages"]
                    )
                )
                self.check("pending_exit")
                raise

        return observe

    def run(self):
        self.stage = "private-store"
        self.validate(initial=True)
        self.check("private_store")
        self.stage = "native-version"
        self.f.prepare_native_cli(self.root, self.binary)
        self.check("native_version")
        self.launch(self.graph)
        self.launch(self.readiness)
        self.lease.join(5)
        self.launch(self.background)
        original = self.box.sync_sessions
        try:
            self.launch(
                self.shell, expected=1, observe_sync=self.pending_observer(original)
            )
        finally:
            finish_actions(
                (
                    lambda: self.gate.release() if hasattr(self, "gate") else None,
                    lambda: (
                        self.shell_worker.join(25)
                        if hasattr(self, "shell_worker")
                        else None
                    ),
                ),
                self.secondary,
            )
        self.stage = "recovery-sync"
        self.validate()
        code = self.box.main(["sync", "--workspace", self.data["id"]])
        self.exits.append(code)
        require(code == 0, "launcher-exit")
        self.stopped()
        private = self.box.read_json(self.archive_path(self.shell_id))
        self.box.verify_host_archive(self.path, self.data, private)
        require(
            any(
                message.get("type") == "shell"
                and message.get("status") == "exited"
                and message.get("exit") == 0
                for message in private["messages"]
            )
        )
        require(
            self.box.session_state(self.path)["synced"][self.shell_id]
            == file_hash(self.archive_path(self.shell_id))
        )
        self.check("sync_recovery")
        self.check("owner_cleanup")
        self.same_identity()
        self.check("unchanged_identity")
        self.check("host_clean")
        self.stage = "complete"


class ShellGate:
    def __init__(self, harness, *, marker="SYNTHETIC_HISTORY_SHELL_DONE"):
        self.harness = harness
        self.path = "/tmp/oc-box-history-" + secrets.token_hex(16)
        self.released = False
        self.marker = marker
        harness.container_python(
            "import json,os,sys\np=json.load(sys.stdin);os.mkfifo(p,0o600)\n", self.path
        )

    def command(self):
        code = (
            "import os,select,sys;f=os.open(sys.argv[1],os.O_RDWR|os.O_NONBLOCK);ready=select.select([f],[],[],120)[0];assert ready;assert os.read(f,1)==b'x';os.close(f);print("
            + repr(self.marker)
            + ")"
        )
        return shlex.join(["python3", "-c", code, self.path])

    def release(self):
        if not self.released:
            self.harness.container_python(
                "import errno,json,os,sys,time\np=json.load(sys.stdin)\nfor _ in range(200):\n try:f=os.open(p,os.O_WRONLY|os.O_NONBLOCK)\n except OSError as e:\n  if e.errno!=errno.ENXIO:raise\n  time.sleep(0.05);continue\n os.write(f,b'x');os.close(f);break\nelse:raise SystemExit(1)\n",
                self.path,
            )
            self.released = True


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "session-history fixture: category=invalid-options\n")


def parser():
    value = ArgumentParser(description=__doc__)
    value.add_argument("--source", type=Path, required=True)
    value.add_argument("--expect-commit", required=True)
    value.add_argument("--work-dir", type=Path, required=True)
    value.add_argument("--temp-dir", type=Path)
    value.add_argument("--opencode-v2", type=Path, required=True)
    value.add_argument("--distro", default="tumbleweed")
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    root = loaded = harness = provider = None
    primary, secondary, failure = None, [], []
    old_umask = os.umask(0o077)
    output = os.fdopen(os.dup(1), "w", buffering=1)
    try:
        host_guard()
        require(sys.flags.isolated and sys.dont_write_bytecode, "invalid-options")
        runner_digest = file_hash(__file__)
        parent = fresh_parent(args.work_dir)
        temporary = fresh_parent(args.temp_dir) if args.temp_dir is not None else None
        source = safe_path(args.source)
        require(
            not any(
                item is not None and item.is_relative_to(source)
                for item in (parent, temporary)
            ),
            "placement",
        )
        loaded = load_source(args.source, args.expect_commit)
        require(file_hash(__file__) == runner_digest, "source-changed")
        f = loaded.fixture
        f.host_guard()
        require(args.distro in loaded.box.DISTROS, "invalid-options")
        try:
            binary = loaded.guards.native_identity(
                args.opencode_v2.expanduser(), outside=(), resolve_entry=True
            ).path
        except loaded.guards.GuardError as error:
            raise Refusal("fixture-refusal") from error
        require(not binary.is_relative_to(loaded.source), "placement")
        native_podman = shutil.which(
            "podman", path=loaded.box.maintenance_host_env().get("PATH", os.defpath)
        )
        require(native_podman is not None, "fixture-only")
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        root = Path(tempfile.mkdtemp(prefix=f.PREFIX + "history-", dir=parent))
        root.chmod(0o700)
        print("Retained history root: " + str(root), file=output)
        harness = Harness(loaded, root, binary, runner_digest)
        harness.distro = args.distro
        harness.progress = lambda name: print(
            "session-history fixture: " + name, file=output, flush=True
        )
        with f.private_output():
            f.prepare_root(root, args.distro)
            f.prepare_podman_cli(root, Path(native_podman).resolve(strict=True))
            f.write_json_new(root / MARKER, harness.purpose())
            provider = Provider()
            harness.provider = provider
            f.write_json_new(root / "config/opencode/opencode.json", provider.config())
            environment = f.isolated_env(root)
            environment["LEMONADE_BASE_URL"] = (
                f"http://host.containers.internal:{provider.server.server_port}/api/v1"
            )
            with (
                patch.dict(os.environ, environment, clear=True),
                f.temporary_location(root, temporary),
            ):
                harness.run()
    except BaseException as error:  # noqa: BLE001 -- Only fixed public categories escape.
        primary = category(error)
        failure = trace(error)
    finally:
        if provider is not None:
            try:
                provider.close()
            except BaseException as error:  # noqa: BLE001 -- Keep shutdown failures secondary.
                secondary.append(category(error))
        if harness is not None:
            try:
                secondary.extend(harness.secondary)
                report = result(
                    harness.checks,
                    loaded.identity,
                    harness.runner,
                    harness.native,
                    primary,
                    secondary,
                    harness.exits,
                    stage=harness.stage,
                    failure_trace=failure,
                )
                loaded.fixture.write_json_new(root / REPORT, report)
                primary = report["primary_category"]
            except BaseException as error:  # noqa: BLE001 -- Reporting cannot reveal private errors.
                secondary.append(category(error))
        os.umask(old_umask)
    passed = primary is None and not secondary
    print(
        "session-history fixture: "
        + ("PASS" if passed else "FAIL category=" + (primary or secondary[0])),
        file=output,
    )
    for entry in failure:
        print(
            "session-history fixture: trace "
            + entry["type"]
            + " "
            + " ".join(entry["frames"]),
            file=output,
        )
    if root is not None:
        print("Report: " + str(root / REPORT), file=output)
    output.close()
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
