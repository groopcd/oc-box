"""Native 2.3.x/2.4.x retained-subagent Reader checks, not Podman acceptance.

Run with python3 -B tests/smoke_history_subagent_recovery_v2.py
--opencode /path/to/opencode --temp-dir /short/private/temporary/directory.
Ten real native subagent jobs are created through a bounded loopback provider.
The producer closes gracefully before the production Reader opens the synthetic
store. Only Podman exec is replaced: Reader, sync, archives, native imports,
fresh destination verification and source/destination guards remain real.
One separately stopped destination subagent/claim must survive standalone
inventory, archive replacement, import and verification without resumption.

Optional --foreground also interrupts an independent genuinely active provider
turn with native queued input, on both source and destination. --producer-close
abrupt kills/reaps only the owned source producer; graceful EOF is the default.
Pending foreground exports retain their previous verified archive/checkpoint;
independent sessions must still progress. No queued input or claim may change.

This local process barrier is NOT the whole-container stop/same-ID restart
proof. Retained jobs remain resumable native metadata, not completed work.
No image-job allowance or diagnosis of a user's timeout is established here.
"""

import argparse
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

from smoke_history_recovery_v2 import (
    CANARY,
    LocalExec,
    SCHEDULE_CASES,
    durable_schedule,
    environment,
    expect_schedule_refusal,
    history_reader,
    history_recovery,
    persisted_history,
    rows,
    subreaper,
)
from smoke_session_sync_v2 import (
    BackgroundProvider,
    FixtureFailure,
    bounded,
    box,
    bridge,
    compat,
    require,
    seed,
    workspace_service,
)
from oc_box import runtime_safeguard


class SubagentProvider(BackgroundProvider):
    """Native shell canary, ten background children, and a settled parent."""

    def __init__(self, children, command, project, *, foreground=False):
        self.children, self.command, self.project = children, command, project
        self.requests = self.errors = self.children_entered = 0
        self.foreground_entered = 0
        self.parent_started = False
        self.lock = threading.Lock()
        self.release = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                try:
                    self.connection.settimeout(10)
                    size = int(self.headers.get("Content-Length", "0"))
                    require(
                        self.path == "/v1/chat/completions" and 0 < size < 2_000_000,
                        "Synthetic subagent provider contract changed.",
                    )
                    body = json.loads(self.rfile.read(size))
                    require(
                        body.get("stream") is True
                        and body.get("model") == "gpt-history",
                        "Synthetic subagent provider request changed.",
                    )
                    with outer.lock:
                        outer.requests += 1
                        require(
                            outer.requests <= children + 2 + int(foreground),
                            "Unexpected synthetic subagent provider turn.",
                        )
                    held_foreground = any(
                        item.get("role") == "user"
                        and "SYNTHETIC_READER_HELD_FOREGROUND"
                        in str(item.get("content", ""))
                        for item in body.get("messages", [])
                    )
                    child = any(
                        item.get("role") == "user"
                        and "SYNTHETIC_READER_HELD_CHILD"
                        in str(item.get("content", ""))
                        for item in body.get("messages", [])
                    )
                    if held_foreground:
                        require(foreground, "Unexpected foreground provider request.")
                        delta, finish = (
                            {"content": "SYNTHETIC_FOREGROUND_INCOMPLETE"},
                            "stop",
                        )
                    elif child:
                        with outer.lock:
                            outer.children_entered += 1
                        require(
                            outer.release.wait(60),
                            "Synthetic child provider hold timed out.",
                        )
                        delta, finish = {"content": "SYNTHETIC_CHILD_RELEASED"}, "stop"
                    else:
                        with outer.lock:
                            initial = not outer.parent_started
                            outer.parent_started = True
                        delta, finish = (
                            outer.tools(body)
                            if initial
                            else ({"content": "SYNTHETIC_PARENT_SETTLED"}, "stop")
                        )
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for part, end in ((delta, None), ({}, finish)):
                        payload = {
                            "id": "chatcmpl-fixture-subagent",
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
                        if held_foreground and end is None:
                            # A held request alone may leave only prior settled
                            # history projected. Deliver real text before holding
                            # the unfinished stream, never its finish or DONE.
                            self.wfile.flush()
                            with outer.lock:
                                outer.foreground_entered += 1
                            require(
                                outer.release.wait(60),
                                "Synthetic foreground provider hold timed out.",
                            )
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass  # The stopped producer no longer owns these requests.
                except Exception:  # noqa: BLE001 -- Never serialize provider input.
                    with outer.lock:
                        outer.errors += 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tools(self, body):
        tools = {
            item["function"]["name"]: item["function"] for item in body.get("tools", [])
        }
        properties = (
            tools.get("subagent", {}).get("parameters", {}).get("properties", {})
        )
        require(
            all(
                properties.get(name, {}).get("type") == "string"
                for name in ("agent", "description", "prompt")
            )
            and tools.get("shell", {})
            .get("parameters", {})
            .get("properties", {})
            .get("background", {})
            .get("type")
            == "boolean",
            "Native 2.3 subagent/shell tool schemas changed.",
        )
        calls = [
            (
                "shell",
                {
                    "command": self.command,
                    "workdir": str(self.project),
                    "background": False,
                },
            )
        ] + [
            (
                "subagent",
                {
                    "agent": "general",
                    "description": "Synthetic retained child",
                    "prompt": "SYNTHETIC_READER_HELD_CHILD",
                },
            )
            for _ in range(self.children)
        ]
        return {
            "tool_calls": [
                {
                    "index": number,
                    "id": "call_fixture_subagent_" + str(number),
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
                for number, (name, arguments) in enumerate(calls)
            ]
        }, "tool_calls"


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def execution_snapshot(database):
    """Bind the actual scheduler layout and all native claims/inbox/history rows."""
    snapshot = persisted_history(database)
    with contextlib.closing(
        sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)
    ) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        for table in ("session_v2", "session_inbox"):
            snapshot[table] = sorted(
                connection.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr
            )
        columns = {
            row[1]: number
            for number, row in enumerate(
                connection.execute("PRAGMA table_info(session_v2)")
            )
        }
    return snapshot, columns


def destination_snapshot(database, original_ids):
    """Keep original destination rows exact while namespaced archives are added."""
    snapshot, _ = execution_snapshot(database)
    with contextlib.closing(
        sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)
    ) as connection:
        connection.execute("PRAGMA query_only=ON")
        for table in ("session_v2", "session_message", "session_part"):
            columns = {
                row[1]: number
                for number, row in enumerate(
                    connection.execute(f'PRAGMA table_info("{table}")')
                )
            }
            key = "id" if table == "session_v2" else "session_id"
            snapshot[table] = [
                row for row in snapshot[table] if row[columns[key]] in original_ids
            ]
    return snapshot


def expect_command_refusal(callback):
    try:
        callback()
    except box.BoxError as error:
        require(
            getattr(error, "history_reader_category", None) == "client-command",
            "Reader command allowlist returned a different refusal.",
        )
        return
    raise FixtureFailure("Reader admitted a non-history command or service flag.")


def hold_foreground(native, provider, project, ident, until):
    """Use only supported native admission, never fabricate inbox/claim rows."""
    seed(native, ident, project)
    previous = bridge.checked_export(native, ident)
    native(
        "POST",
        "/api/session/" + ident + "/prompt",
        {
            "id": "msg_" + ident.removeprefix("ses_") + "_held",
            "text": "SYNTHETIC_READER_HELD_FOREGROUND",
            "delivery": "queue",
            "resume": True,
        },
    )

    until(
        lambda: (
            provider.foreground_entered == 1
            and ident in native("GET", "/api/session/active")["data"]
        )
    )
    queued_id = "msg_" + ident.removeprefix("ses_") + "_queued"
    admitted = native(
        "POST",
        "/api/session/" + ident + "/prompt",
        {
            "id": queued_id,
            "text": "SYNTHETIC_READER_QUEUED_NEVER_EXECUTE",
            "delivery": "queue",
            "resume": False,
        },
    )["data"]
    require(
        admitted["id"] == queued_id
        and admitted["sessionID"] == ident
        and admitted["type"] == "user"
        and admitted["delivery"] == "queue"
        and native("GET", "/api/session/" + ident + "/inbox")["data"] == [admitted]
        and ident in native("GET", "/api/session/active")["data"],
        "Foreground hold or genuine native queued admission was not established.",
    )
    try:
        # A genuinely active streamed turn need not yet have a durable unfinished
        # assistant projection. Check whichever history native actually exposes;
        # never manufacture a pending row merely to satisfy the fixture.
        bridge.checked_export(native, ident)
    except bridge.FixedError as error:
        require(
            str(error) == bridge.UNSETTLED, "Live foreground export failed differently."
        )
    return previous, admitted


def require_queued_claim(database, snapshot, columns, ident, admitted):
    """Bind the nonempty native inbox and the foreground execution claim."""
    with contextlib.closing(
        sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)
    ) as connection:
        inbox_columns = {
            row[1]: number
            for number, row in enumerate(
                connection.execute("PRAGMA table_info(session_inbox)")
            )
        }
    require(
        len(snapshot["session_inbox"]) == 1
        and snapshot["session_inbox"][0][inbox_columns["id"]] == admitted["id"]
        and snapshot["session_inbox"][0][inbox_columns["session_id"]] == ident
        and snapshot["session_inbox"][0][inbox_columns["delivery"]] == "queue"
        and snapshot["session_inbox"][0][inbox_columns["type"]] == "user"
        and json.loads(snapshot["session_inbox"][0][inbox_columns["payload"]])
        == admitted["payload"]
        and any(
            row[columns["id"]] == ident and row[columns["time_suspended"]] is not None
            for row in snapshot["session_v2"]
        ),
        "Stopped foreground lost its queued input or durable execution claim.",
    )


def guarded_reader_executable(binary, release, root, path, data, environment, digest):
    """Prove the guarded reader checks the bytes, not just the native version."""
    prefix = root / "guarded-runtime"
    executable = prefix / "lib/node_modules/@opencode/cli/bin/opencode.exe"
    executable.parent.mkdir(parents=True)
    shutil.copyfile(binary, executable)
    executable.chmod(0o700)
    alias = prefix / "bin/opencode"
    alias.parent.mkdir()
    alias.symlink_to("../lib/node_modules/@opencode/cli/bin/opencode.exe")
    marker = root / "shadow-interpreter-ran"
    shadow = alias.with_name("python3")
    shadow.write_text(f"#!/bin/sh\n: > {shlex.quote(str(marker))}\nexit 42\n")
    shadow.chmod(0o700)
    shadowed_path = str(alias.parent) + ":/usr/bin:/bin"
    changed = executable.with_suffix(".changed")
    shutil.copyfile(executable, changed)
    changed.chmod(0o700)
    # Change an unused ELF identification padding byte on a fixture copy.
    # The independent version probe must still agree with the exact patch.
    with changed.open("r+b") as stream:
        stream.seek(9)
        old = stream.read(1)
        require(len(old) == 1, "The pinned ELF header was incomplete.")
        stream.seek(9)
        stream.write(bytes([old[0] ^ 1]))
    require(
        file_digest(changed) != digest
        and compat.version(bounded([str(changed), "--version"], environment, root))
        == release,
        "The replacement fixture did not report the same exact version.",
    )
    deadline = time.monotonic() + 40

    def reader():
        return history_reader.Reader(
            box,
            data,
            remaining=lambda: deadline - time.monotonic(),
            runtime_digest=digest,
        )

    saved = executable.with_suffix(".retained")
    with (
        patch.dict(os.environ, environment, clear=True),
        patch.object(box, "CONTAINER_OPENCODE", str(alias)),
        patch.object(runtime_safeguard, "PREFIX", str(prefix)),
        patch.object(history_reader, "PATH", shadowed_path),
        box.workspace_lock(path),
    ):
        with reader() as original:
            worker = original.command(
                [
                    "python3",
                    "-I",
                    "-S",
                    "-c",
                    (box.PACKAGE / "opencode_v2_io.py").read_text(),
                    "history-worker",
                    str(alias),
                ]
            )
            require(
                "python3" not in worker and worker.count("/usr/bin/python3") == 4,
                "The pass-local reader helper did not use only trusted interpreters.",
            )
            executable.replace(saved)
            changed.replace(executable)
            try:
                try:
                    original.assert_idle()
                except box.BoxError as error:
                    require(
                        getattr(error, "history_reader_category", None)
                        == "active-query",
                        "A replaced reader client failed outside its native launch boundary.",
                    )
                else:
                    raise FixtureFailure(
                        "A reader helper used new bytes under the same version."
                    )
            finally:
                executable.replace(changed)
                saved.replace(executable)
        changed.replace(executable)
        try:
            with reader():
                raise FixtureFailure(
                    "A guarded reader started native on replaced bytes."
                )
        except box.BoxError as error:
            category = getattr(error, "history_reader_category", None)
            fixed = (
                category
                if isinstance(category, str) and category in history_reader.CATEGORIES
                else "unexpected"
            )
            require(
                fixed == "runtime-executable-unverified",
                f"The guarded reader did not report the fixed executable refusal ({fixed}).",
            )
        require(
            history_reader.current() is None
            and workspace_service.live_connection() is None,
            "The guarded executable refusal retained a reader connection.",
        )
        require(
            not marker.exists(), "A guarded helper used the runtime PATH interpreter."
        )


def exercise(binary, root, children=10, *, foreground=False, producer_close="graceful"):
    """Return the routing hint separately from the native Reader proof.

    The normal CLI below requires the fixed hint. Keeping that final assertion
    separate also permits a source-pinned, pre-fix Reader investigation without
    changing production code or falsely reporting an integrated fixture pass.
    """
    require(children in (1, 10), "Only one or ten synthetic children are supported.")
    require(
        producer_close in ("graceful", "abrupt")
        and (foreground or producer_close == "graceful"),
        "Abrupt producer loss requires the optional foreground variant.",
    )
    project = root / "project"
    project.mkdir()
    source_env = environment(root / "source")
    host_env = environment(root / "destination")
    host_env.pop("OPENCODE_DB")
    release = compat.version(bounded([binary, "--version"], source_env, project))
    require(
        history_reader.snapshot_profile(release)
        in history_reader.SUBAGENT_SNAPSHOT_PROFILES,
        "This fixture requires unmodified stable native 2.3.x or 2.4.x.",
    )
    native_digest = file_digest(binary)
    bounded(["git", "init", "--quiet"], source_env, project)
    data = {
        "id": "d" * 32,
        "project": str(project),
        "home": source_env["HOME"],
        "container": "f" * 64,
        "opencode_version": release,
    }
    path = root / "registry" / "workspaces" / data["id"] / "workspace.json"
    box.atomic_json(path, data)
    (path.parent / "controller").mkdir()
    host_env["OC_BOX_STATE_HOME"] = str(root / "registry")
    host_env.update(PRIVATE_PROVIDER_TOKEN=CANARY, OPENCODE_AUTH_CONTENT=CANARY)
    activations, shell_starts = root / "plugin-starts", root / "shell-starts"
    shell_script = root / "shell-canary.py"
    shell_script.write_text(
        "from pathlib import Path\n"
        f"with Path({str(shell_starts)!r}).open('ab') as stream: stream.write(b'x')\n"
        "print('SYNTHETIC_READER_SHELL_CANARY')\n"
    )
    plugin = root / "plugin"
    plugin.mkdir()
    (plugin / "package.json").write_text(
        json.dumps({"type": "module", "exports": "./index.js"})
    )
    (plugin / "index.js").write_text(
        "import {appendFileSync} from 'node:fs';\n"
        "export default {id:'fixture.subagent-reader',async setup(){"
        + "appendFileSync("
        + json.dumps(str(activations))
        + ",'x');}};\n"
    )
    provider = SubagentProvider(
        children,
        shlex.join([sys.executable, str(shell_script)]),
        project,
        foreground=foreground,
    )
    config = json.loads(provider.config(source_env["OPENCODE_CONFIG_CONTENT"]))
    config["plugins"] = [{"package": str(plugin)}]
    config["policies"].append(
        {"action": "plugin.use", "resource": "*", "effect": "allow"}
    )
    source_env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    # These exact saved/project canaries are exercised by the producer. Reader
    # and destination commands must suppress them, not rely on inert examples.
    for env in (source_env, host_env):
        saved = Path(env["XDG_CONFIG_HOME"]) / "opencode/opencode.json"
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_text(json.dumps(config))
    (project / "opencode.json").write_text(json.dumps(config))
    local = LocalExec(data, binary)
    process = destination_process = None

    def stop_owned_client(child):
        if child is not None and child.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)

    def until(predicate):
        deadline = time.monotonic() + 20
        while not predicate():
            require(
                provider.errors == 0 and time.monotonic() < deadline,
                "Native subagent fixture did not reach its bounded hold point.",
            )
            time.sleep(0.05)

    with contextlib.ExitStack() as cleanup:
        cleanup.callback(provider.close)
        cleanup.callback(lambda: stop_owned_client(process))
        cleanup.callback(lambda: stop_owned_client(destination_process))
        cleanup.callback(provider.release.set)
        cleanup.enter_context(patch.object(subprocess, "Popen", new=local))
        cleanup.enter_context(patch.object(box, "CONTAINER_OPENCODE", binary))
        producer_exit = {}
        foreground_ident = "ses_fixture_source_foreground"
        foreground_hint = None
        with (
            patch.dict(os.environ, source_env, clear=True),
            bridge.server(binary, exit_info=producer_exit) as old,
        ):
            parent = seed(old, "ses_fixture_subagent_parent", project)
            independent = seed(old, "ses_fixture_subagent_independent", project)
            process = subprocess.Popen(
                [
                    binary,
                    "run",
                    "--server",
                    old.endpoint["url"],
                    "--session",
                    parent,
                    "--model",
                    "fixture/gpt-history",
                    "--agent",
                    "build",
                    "SYNTHETIC_READER_PARENT",
                ],
                env={**source_env, "OPENCODE_PASSWORD": old.endpoint["password"]},
                cwd=project,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            def held_children():
                active = old("GET", "/api/session/active")["data"]
                jobs = old("GET", "/api/experimental/job")["data"]
                return (
                    provider.children_entered == children
                    and len(jobs) == children
                    and parent not in active
                    and all(
                        job["kind"] == "subagent"
                        and job["status"] == "running"
                        and job["sessionID"] == parent
                        and job["childSessionID"] in active
                        for job in jobs
                    )
                    and shell_starts.exists()
                )

            until(held_children)
            original_jobs = old("GET", "/api/experimental/job")["data"]
            job_ids = {job["id"] for job in original_jobs}
            child_ids = {job["childSessionID"] for job in original_jobs}
            parent_export = bridge.checked_export(old, parent)
            independent_before = bridge.checked_export(old, independent)
            old(
                "PATCH",
                "/api/session/" + independent,
                {"title": "Synthetic independent updated before stop"},
            )
            require(
                len(child_ids) == children
                and provider.requests == children + 2
                and shell_starts.read_bytes() == b"x"
                and activations.exists(),
                "Native provider, subagent or execution canaries were not proven.",
            )
            if foreground:
                foreground_before, source_queued = hold_foreground(
                    old, provider, project, foreground_ident, until
                )
                live_activations = activations.read_bytes()

                def live_client(_box, _data, _connection, arguments, **_kwargs):
                    return list(arguments)

                # Exercise the genuine live timeout BEFORE closing the producer.
                # The hint is routing only: normal readiness must still refuse.
                with (
                    patch.dict(
                        os.environ, {"OPENCODE_PASSWORD": old.endpoint["password"]}
                    ),
                    patch.object(
                        workspace_service, "client_command", side_effect=live_client
                    ),
                ):
                    try:
                        workspace_service.wait_history(
                            box, data, old.endpoint, timeout=3, interval=0.1
                        )
                    except box.HistoryWaitTimeout as error:
                        foreground_hint = (
                            error.recovery_eligible is True
                            and error.interrupted_foreground is True
                        )
                        require(
                            f"foreground={children + 1}" in error.diagnostic
                            and f"jobs-running={children}" in error.diagnostic,
                            "Live timeout lost the actual foreground/job observation.",
                        )
                    else:
                        raise FixtureFailure(
                            "Normal readiness waived active foreground."
                        )
                require(
                    foreground_ident in old("GET", "/api/session/active")["data"]
                    and provider.requests == children + 3
                    and provider.foreground_entered == 1
                    and shell_starts.read_bytes() == b"x"
                    and activations.read_bytes() == live_activations
                    and old("GET", "/api/session/" + foreground_ident + "/inbox")[
                        "data"
                    ]
                    == [source_queued],
                    "Live history wait consumed queued input or ended the provider hold.",
                )
                if producer_close == "abrupt":
                    producer = local.producers[-1]
                    require(producer.poll() is None, "Source producer already exited.")
                    os.killpg(producer.pid, signal.SIGKILL)
                    producer.wait(timeout=5)
        require(
            not old.alive()
            and producer_exit
            == {
                "code": 0 if producer_close == "graceful" else -signal.SIGKILL,
                "forced": False,
            },
            "Original native producer did not close in the selected mode and reap.",
        )
        # A run client may outlive its final parent response while children run.
        # It is a client only; the selected producer/reap barrier stays real.
        stop_owned_client(process)
        require(process.poll() is not None, "Original native client was not reaped.")
        provider.release.set()
        source_db = Path(source_env["XDG_DATA_HOME"]) / "opencode/oc-box-v2.db"
        persisted, columns = execution_snapshot(source_db)
        require(
            len(persisted["job"]) == children
            and persisted["schedule"] == []
            and (foreground or persisted["session_inbox"] == []),
            "Unexpected post-stop jobs, scheduler or queued input.",
        )
        if foreground:
            require_queued_claim(
                source_db, persisted, columns, foreground_ident, source_queued
            )
        source_sessions = {row[columns["id"]]: row for row in persisted["session_v2"]}
        require(
            all(
                source_sessions[ident][columns["time_suspended"]] is not None
                for ident in child_ids
            ),
            "Native shutdown did not retain the children's execution claims.",
        )
        # A separately stopped destination producer leaves one real resumable
        # child BEFORE any standalone destination history command is tested.
        destination_provider = SubagentProvider(
            1,
            shlex.join([sys.executable, str(shell_script)]),
            project,
            foreground=foreground,
        )
        cleanup.callback(destination_provider.close)
        destination_config = json.loads(
            destination_provider.config(host_env["OPENCODE_CONFIG_CONTENT"])
        )
        destination_config["plugins"] = [{"package": str(plugin)}]
        destination_config["policies"].append(
            {"action": "plugin.use", "resource": "*", "effect": "allow"}
        )
        destination_saved = Path(host_env["XDG_CONFIG_HOME"]) / "opencode/opencode.json"
        destination_saved.write_text(json.dumps(destination_config))
        destination_env = {
            key: value
            for key, value in host_env.items()
            if key not in ("PRIVATE_PROVIDER_TOKEN", "OPENCODE_AUTH_CONTENT")
        }
        destination_env["OPENCODE_CONFIG_CONTENT"] = json.dumps(destination_config)
        destination_exit = {}
        with (
            patch.dict(os.environ, destination_env, clear=True),
            bridge.server(binary, exit_info=destination_exit) as destination,
        ):
            destination_parent = seed(
                destination, "ses_fixture_destination_parent", project
            )
            destination_process = subprocess.Popen(
                [
                    binary,
                    "run",
                    "--server",
                    destination.endpoint["url"],
                    "--session",
                    destination_parent,
                    "--model",
                    "fixture/gpt-history",
                    "--agent",
                    "build",
                    "SYNTHETIC_READER_DESTINATION_PARENT",
                ],
                env={
                    **destination_env,
                    "OPENCODE_PASSWORD": destination.endpoint["password"],
                },
                cwd=project,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            def destination_held():
                active = destination("GET", "/api/session/active")["data"]
                jobs = destination("GET", "/api/experimental/job")["data"]
                return (
                    destination_provider.children_entered == 1
                    and len(jobs) == 1
                    and destination_parent not in active
                    and jobs[0]["kind"] == "subagent"
                    and jobs[0]["status"] == "running"
                    and jobs[0]["sessionID"] == destination_parent
                    and jobs[0]["childSessionID"] in active
                )

            until(destination_held)
            destination_job = destination("GET", "/api/experimental/job")["data"][0]
            destination_ids = {destination_parent, destination_job["childSessionID"]}
            require(
                destination_provider.requests == 3
                and destination_provider.errors == 0
                and shell_starts.read_bytes() == b"xx",
                "Destination provider/subagent/shell canaries were not proven.",
            )
            if foreground:
                destination_foreground = "ses_fixture_destination_foreground"
                _, destination_queued = hold_foreground(
                    destination,
                    destination_provider,
                    project,
                    destination_foreground,
                    until,
                )
                destination_ids.add(destination_foreground)
                require(
                    destination_provider.requests == 4
                    and destination_provider.errors == 0,
                    "Destination foreground provider was not independently held.",
                )
        require(
            not destination.alive()
            and destination_exit == {"code": 0, "forced": False}
            and len(local.producers) == 2,
            "Destination producer did not close gracefully and reap.",
        )
        stop_owned_client(destination_process)
        require(
            destination_process.poll() is not None,
            "Original destination client was not reaped.",
        )
        destination_provider.release.set()
        host_db = Path(
            bounded(
                [binary, "debug", "paths", "db"], destination_env, project
            ).removesuffix("\n")
        )
        require(
            host_db.is_relative_to(Path(host_env["XDG_DATA_HOME"])),
            "Destination producer escaped its fresh private store.",
        )
        destination_persisted = destination_snapshot(host_db, destination_ids)
        require(
            len(destination_persisted["job"]) == 1
            and destination_persisted["schedule"] == []
            and (foreground or destination_persisted["session_inbox"] == []),
            "Destination did not retain one native subagent with empty scheduling.",
        )
        _, destination_columns = execution_snapshot(host_db)
        if foreground:
            require_queued_claim(
                host_db,
                destination_persisted,
                destination_columns,
                destination_foreground,
                destination_queued,
            )
        require(
            any(
                row[destination_columns["id"]] == destination_job["childSessionID"]
                and row[destination_columns["time_suspended"]] is not None
                for row in destination_persisted["session_v2"]
            ),
            "Destination child execution claim was not retained.",
        )
        executions = (
            provider.requests,
            destination_provider.requests,
            shell_starts.read_bytes(),
            activations.read_bytes(),
        )

        def no_execution():
            require(
                (
                    provider.requests,
                    destination_provider.requests,
                    shell_starts.read_bytes(),
                    activations.read_bytes(),
                )
                == executions
                and provider.errors == destination_provider.errors == 0,
                "Reader or guards executed a provider, shell or user plugin.",
            )

        local.history_only = True
        # Compare original rows from BEFORE the first genuine standalone host
        # export, so a premature resume cannot be hidden by a late baseline.
        destination_exports = {}
        history_reader._check_database(host_db, version=release)
        destination_fence = history_reader.fence_environment(
            destination_env, version=release
        )
        for ident in destination_ids:
            # Native's own export may omit an unfinished foreground tail. This
            # is a destination preservation sentinel, not checked archival of
            # that tail; the full database snapshot also has to remain exact.
            destination_exports[ident] = json.loads(
                bounded(
                    compat.command(release, binary, ("--pure", "export", ident)),
                    destination_fence,
                    path.parent / "controller",
                )
            )
        # Establish one genuine previous controller archive, then let unchanged
        # sync_sessions replace it from the newer stopped source snapshot. This
        # covers standalone child inventory/delete/import, not just first import.
        previous_archive = box.archive_session(independent_before, data)
        previous_target = (
            path.parent / "history" / (previous_archive["info"]["id"] + ".json")
        )
        box.atomic_json(previous_target, previous_archive)
        bounded(
            compat.command(release, binary, ("--pure", "import", str(previous_target))),
            destination_fence,
            path.parent / "controller",
        )
        previous_digest = hashlib.sha256(previous_target.read_bytes()).hexdigest()
        box.atomic_json(
            box.session_file(path),
            {
                "sessions": [independent],
                "last": None,
                "synced": {independent: previous_digest},
                "host_sessions": {independent: previous_archive["info"]["id"]},
            },
        )
        if foreground:
            previous_foreground = box.archive_session(foreground_before, data)
            foreground_target = (
                path.parent / "history" / (previous_foreground["info"]["id"] + ".json")
            )
            box.atomic_json(foreground_target, previous_foreground)
            foreground_archive_bytes = foreground_target.read_bytes()
            foreground_digest = hashlib.sha256(foreground_archive_bytes).hexdigest()
            bounded(
                compat.command(
                    release, binary, ("--pure", "import", str(foreground_target))
                ),
                destination_fence,
                path.parent / "controller",
            )
            prior_state = box.session_state(path)
            prior_state["sessions"].append(foreground_ident)
            prior_state["synced"][foreground_ident] = foreground_digest
            prior_state["host_sessions"][foreground_ident] = previous_foreground[
                "info"
            ]["id"]
            box.atomic_json(box.session_file(path), prior_state)
        require(
            destination_snapshot(host_db, destination_ids) == destination_persisted,
            "Initial standalone host exports resumed or changed destination work.",
        )
        no_execution()
        deadline = time.monotonic() + 150

        def remaining():
            budget = deadline - time.monotonic()
            require(budget > 0, "Subagent Reader exhausted its bounded budget.")
            return budget

        def host_command(_data, *arguments):
            return compat.command(release, binary, arguments)

        stop_token = history_recovery.workspace_control.capture(path)
        snapshot_source = history_recovery.Source(data["container"], stop_token, ())
        recovery_hint = None
        with (
            patch.dict(os.environ, host_env, clear=True),
            patch.object(box, "host_command", side_effect=host_command),
            box.workspace_lock(path),
            history_recovery.budget(150),
            history_recovery.commands(path, snapshot_source),
            history_reader.Reader(box, data, remaining=remaining) as reader,
        ):
            require(
                history_reader.current() is reader
                and workspace_service.live_connection() == reader.connection,
                "Production Reader did not install its single endpoint context.",
            )
            fresh = bridge.endpoint_request(reader.connection)
            reader_pid = fresh("GET", "/api/info")["pid"]
            require(
                fresh("GET", "/api/session/active")["data"] == {}
                and fresh("GET", "/api/experimental/job")["data"] == original_jobs,
                "Reader changed retained jobs or started foreground execution.",
            )
            no_execution()

            def local_client(_box, _data, _connection, arguments, **_kwargs):
                return reader.command(arguments)

            with patch.object(
                workspace_service, "client_command", side_effect=local_client
            ):
                try:
                    workspace_service.wait_history(
                        box, data, reader.connection, timeout=3, interval=0.1
                    )
                except box.HistoryWaitTimeout as error:
                    recovery_hint = error.recovery_eligible
                    if foreground:
                        require(
                            error.interrupted_foreground is False,
                            "Retained idle jobs were misclassified as live foreground.",
                        )
                    require(
                        all(
                            field in error.diagnostic
                            for field in (
                                "foreground=0",
                                f"jobs-running={children}",
                                "jobs-completed=0",
                                "jobs-error=0",
                                "jobs-cancelled=0",
                            )
                        ),
                        "Timeout lacked a complete retained-job observation.",
                    )
                else:
                    raise FixtureFailure(
                        "Normal readiness waived retained subagent jobs."
                    )
            reader.assert_idle()
            require(
                bridge.checked_export(fresh, parent) == parent_export,
                "Reader changed the settled parent transcript.",
            )
            for command in (
                [sys.executable, "-c", "raise SystemExit(0)"],
                [binary, "serve", "--stdio"],
                [binary, "service", "restart"],
                [binary, "session", "list", "--standalone"],
            ):
                expect_command_refusal(lambda: reader.command(command))

            expected_ids = child_ids | {parent, independent}
            if foreground:
                expected_ids.add(foreground_ident)
            exports, pending_ids = {}, set()
            for ident in expected_ids:
                try:
                    exports[ident] = bridge.checked_export(fresh, ident)
                except bridge.FixedError as error:
                    require(
                        foreground
                        and ident == foreground_ident
                        and str(error) == bridge.UNSETTLED,
                        "Reader checked export failed outside interrupted foreground.",
                    )
                    pending_ids.add(ident)
            require(
                not any(
                    message.get("metadata", {}).get("jobID") in job_ids
                    for exported in exports.values()
                    for message in exported["messages"]
                ),
                "Reader invented a retained-job completion notice.",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                try:
                    box.sync_sessions(path, data, announce=False)
                except box.BoxError as error:
                    require(
                        pending_ids == {foreground_ident}
                        and str(error)
                        == (
                            "1 session(s) not settled yet: "
                            + bridge.UNSETTLED
                            + " Their last archives are retained and other sessions "
                            "were synchronized."
                        ),
                        "Reader sync failed outside the known pending foreground.",
                    )
                else:
                    require(not pending_ids, "Reader sync concealed pending history.")
            require(
                "Session history synced" not in output.getvalue(),
                "Post-stop snapshot printed ordinary live sync success.",
            )
            state = box.session_state(path)
            if pending_ids:
                require(
                    foreground_target.read_bytes() == foreground_archive_bytes
                    and state["synced"][foreground_ident] == foreground_digest
                    and state["host_sessions"][foreground_ident]
                    == previous_foreground["info"]["id"],
                    "Pending foreground replaced its prior archive or checkpoint.",
                )
                box.verify_host_archive(path, data, previous_foreground)
            require(
                set(state["sessions"]) == expected_ids
                and set(state["synced"]) == expected_ids,
                "Reader sync lost native child or independent-session progress.",
            )
            require(
                [
                    "api",
                    "get",
                    "/api/session?parentID="
                    + previous_archive["info"]["id"]
                    + "&limit=1",
                    "--standalone",
                ]
                in local.host_calls
                and [
                    "session",
                    "delete",
                    previous_archive["info"]["id"],
                    "--standalone",
                ]
                in local.host_calls
                and state["synced"][independent] != previous_digest,
                "Native standalone archive inventory/replacement was not exercised.",
            )
            no_execution()
            for ident, exported in exports.items():
                expected = box.archive_session(exported, data)
                saved = path.parent / "history" / (expected["info"]["id"] + ".json")
                require(
                    box.read_json(saved) == expected
                    and bridge.checked_export(fresh, ident) == exported,
                    "Saved archive or checked source transcript changed.",
                )
                native = box.run(
                    box.host_command(data, "--pure", "export", expected["info"]["id"]),
                    env=box.private_host_env(path, data),
                    cwd=path.parent / "controller",
                    capture=True,
                    private=True,
                    timeout=min(remaining(), 30),
                )
                verified = json.loads(native.stdout)
                require(
                    verified["messages"] == expected["messages"]
                    and verified["info"]["id"] == expected["info"]["id"]
                    and verified["info"].get("parentID") is None,
                    "Fresh native destination verification lost archive identity/content.",
                )
            require(
                local.leases == 1
                and fresh("GET", "/api/info")["pid"] == reader_pid
                and fresh("GET", "/api/session/active")["data"] == {},
                "Recovery opened another source service or started execution.",
            )
            require(
                execution_snapshot(source_db)[0] == persisted,
                "Reader sync changed jobs, execution claims, inbox or history.",
            )
            no_execution()
            discovery = box.run(
                box.host_command(data, "debug", "paths", "db"),
                env=box.private_host_env(path, data),
                capture=True,
                private=True,
                timeout=min(remaining(), 15),
            )
            discovered_db = Path(discovery.stdout.removesuffix("\n"))
            require(
                discovered_db == host_db,
                "Destination discovery changed its private native store.",
            )
            for ident, expected in destination_exports.items():
                native = box.run(
                    box.host_command(data, "--pure", "export", ident),
                    env=box.private_host_env(path, data),
                    cwd=path.parent / "controller",
                    capture=True,
                    private=True,
                    timeout=min(remaining(), 30),
                )
                require(
                    json.loads(native.stdout) == expected,
                    "Standalone history commands changed an existing host transcript.",
                )
            require(
                destination_snapshot(host_db, destination_ids) == destination_persisted,
                "Host imports/verification changed retained jobs, claims or inbox.",
            )
            for status, wal in SCHEDULE_CASES:
                with durable_schedule(
                    host_db, state["host_sessions"][parent], status, wal=wal
                ) as scheduled:
                    calls_before = len(local.host_calls)
                    expect_schedule_refusal(lambda: box.private_host_env(path, data))
                    require(
                        local.host_calls[calls_before:]
                        and all(
                            call == ["--version"]
                            or call[:3] == ["debug", "paths", "db"]
                            for call in local.host_calls[calls_before:]
                        )
                        and rows(host_db, "schedule") == scheduled,
                        "Destination guard projected native data or changed a schedule.",
                    )
            no_execution()
        require(
            history_reader.current() is None
            and workspace_service.live_connection() is None
            and execution_snapshot(source_db)[0] == persisted,
            "Reader shutdown retained context or changed durable source state.",
        )
        reader_leases = local.leases
        require(
            destination_snapshot(host_db, destination_ids) == destination_persisted,
            "Reader shutdown changed original destination execution/history rows.",
        )
        if pending_ids:
            require(
                foreground_target.read_bytes() == foreground_archive_bytes
                and box.session_state(path)["synced"][foreground_ident]
                == foreground_digest,
                "Reader cleanup changed the pending archive or checkpoint.",
            )
        no_execution()
        marker, observed_binary = root / "native-boot", root / "observe-native.py"
        observed_binary.write_text(
            f"#!{sys.executable}\nimport os,pathlib,sys\n"
            f"pathlib.Path({str(marker)!r}).touch()\n"
            f"os.execv({binary!r},[{binary!r},*sys.argv[1:]])\n"
        )
        observed_binary.chmod(0o700)

        def refused_reader():
            with (
                box.workspace_lock(path),
                history_reader.Reader(box, data, remaining=remaining),
            ):
                raise FixtureFailure("Source scheduler guard admitted a native Reader.")

        with (
            patch.dict(os.environ, host_env, clear=True),
            patch.object(box, "CONTAINER_OPENCODE", str(observed_binary)),
        ):
            for status, wal in SCHEDULE_CASES:
                with durable_schedule(source_db, parent, status, wal=wal) as scheduled:
                    expect_schedule_refusal(refused_reader)
                    require(
                        not marker.exists()
                        and rows(source_db, "schedule") == scheduled,
                        "Source guard booted native or changed a retained schedule.",
                    )
        require(
            execution_snapshot(source_db)[0] == persisted
            and history_reader.current() is None
            and workspace_service.live_connection() is None,
            "Source refusals changed claims/history or retained a Reader context.",
        )
        no_execution()
        guarded_bytes_verified = (
            history_reader.snapshot_profile(release) == history_reader.SNAPSHOT_24
            and not foreground
        )
        if guarded_bytes_verified:
            guarded_reader_executable(
                binary, release, root, path, data, host_env, native_digest
            )
            require(
                execution_snapshot(source_db)[0] == persisted,
                "The guarded executable regression changed durable source data.",
            )
            no_execution()
        require(
            file_digest(binary) == native_digest,
            "The native executable changed during the fixture.",
        )
        result = {
            "native_version": release,
            "native_binary_sha256": native_digest,
            "source_scheduler_layout": persisted["scheduler_layout"],
            "destination_scheduler_layout": destination_persisted["scheduler_layout"],
            "children": children,
            "sessions_verified": len(expected_ids),
            "recovery_hint": recovery_hint,
            "producer_exit": producer_exit,
            "destination_producer_exit": destination_exit,
            "reader_leases": reader_leases,
            "destination_retained_subagent_jobs": 1,
            "destination_original_sessions_preserved": len(destination_ids),
            "standalone_archive_replacement_verified": True,
            "source_schedule_refusals": len(SCHEDULE_CASES),
            "destination_schedule_refusals": len(SCHEDULE_CASES),
            "provider_requests": provider.requests,
            "destination_provider_requests": destination_provider.requests,
            "durable_execution_history_unchanged": True,
            "guarded_executable_replacement_refused": guarded_bytes_verified,
            "extra_provider_shell_plugin_execution": False,
        }
        if foreground:
            result.update(
                foreground_interruption=True,
                foreground_recovery_hint=foreground_hint,
                source_producer_close=producer_close,
                source_queued_input_preserved=True,
                destination_queued_input_preserved=True,
                foreground_execution_claims_preserved=True,
                pending_foreground_sessions=len(pending_ids),
                pending_prior_archive_checkpoint_preserved=bool(pending_ids),
                sessions_verified=len(exports),
                all_source_history_archived=not pending_ids,
            )
        return result


def main(
    binary, temp_dir=None, children=10, *, foreground=False, producer_close="graceful"
):
    previous = os.umask(0o077)
    try:
        with (
            subreaper(),
            tempfile.TemporaryDirectory(
                prefix="oc-box-subagent-reader-", dir=temp_dir
            ) as temporary,
        ):
            result = exercise(
                str(Path(binary).resolve()),
                Path(temporary),
                children,
                foreground=foreground,
                producer_close=producer_close,
            )
            require(
                result["recovery_hint"] is True,
                "Native retained subagents lacked the post-stop routing hint.",
            )
            if foreground:
                require(
                    result["foreground_recovery_hint"] is True,
                    "Native live foreground lacked the separate post-stop routing hint.",
                )
    except FixtureFailure:
        raise
    except Exception:
        raise FixtureFailure(
            "Native subagent Reader fixture failed; private diagnostics were suppressed."
        ) from None
    finally:
        os.umask(previous)
    print(
        f"PASS: native {result['native_version']} retained subagents, production Reader, real archive/import "
        "and fresh destination verification; durable claims/jobs/history preserved.",
        flush=True,
    )
    if foreground and result["pending_foreground_sessions"]:
        print(
            "PENDING: interrupted foreground remains unarchived; its prior verified "
            "archive/checkpoint was retained while independent sessions progressed.",
            flush=True,
        )
    print("PROFILE: " + json.dumps(result, sort_keys=True), flush=True)
    print(
        "SCOPE: synthetic local native/offline only; no image-job, user-cause or "
        "whole-container/external-host acceptance claim.",
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True)
    parser.add_argument("--temp-dir")
    parser.add_argument("--children", type=int, choices=(1, 10), default=10)
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument(
        "--producer-close", choices=("graceful", "abrupt"), default="graceful"
    )
    arguments = parser.parse_args()
    if arguments.producer_close == "abrupt" and not arguments.foreground:
        parser.error("--producer-close abrupt requires --foreground")
    try:
        main(
            arguments.opencode,
            arguments.temp_dir,
            arguments.children,
            foreground=arguments.foreground,
            producer_close=arguments.producer_close,
        )
    except FixtureFailure as error:
        print("FAIL: " + str(error), file=sys.stderr)
        raise SystemExit(1) from None
