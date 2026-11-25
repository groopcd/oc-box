#!/usr/bin/env python3
"""Time a real native sync pass on disposable synthetic data, without Podman.

Run from the checkout with PYTHONPATH=src:tests and a stock OpenCode 2.4.1
executable. Only process placement is simulated; source inventory, checked
exports, standalone destination operations and checkpointing are production.
Timings exclude image setup, Podman, and ordinary configuration preparation.
"""

import argparse
from collections import Counter
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from oc_box import history_reader, oc_box, opencode_compat, workspace_service
from smoke_history_source_v24 import native, source_database
from smoke_session_sync_v2 import private_environment, rename, seed


VERSION = "2.4.1"


def load_history(binary, home, project, sessions, messages, text_size):
    filler = ('synthetic text α with \\"escapes\\"\n' + "." * text_size)[:text_size]
    with native(binary, home, VERSION) as request:
        for i in range(sessions):

            def import_session(method, route, payload):
                payload["messages"].extend(
                    {
                        "id": f"msg_bench_{i:04d}_{j:06d}",
                        "type": "user",
                        "text": f"{i}:{j} {filler}",
                        "time": {"created": 10 + j},
                    }
                    for j in range(messages - 1)
                )
                return request(method, route, payload)

            seed(
                import_session,
                f"ses_bench_{i:04d}",
                project,
                parent="ses_bench_0000" if i % 5 == 1 else None,
            )


def benchmark(root, binary, args):
    project, home = root / "synthetic-project", root / "synthetic-home"
    project.mkdir()
    home.mkdir()
    with history_reader._remote_environment(str(home), version=VERSION) as (
        version_cwd,
        version_env,
    ):
        version = subprocess.run(
            [binary, "--version"],
            cwd=version_cwd,
            env=version_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        if opencode_compat.version(version.stdout) != VERSION:
            raise RuntimeError(f"Expected stock native OpenCode {VERSION}")
    load_history(binary, home, project, args.sessions, args.messages, args.text_size)
    print(
        json.dumps(
            {
                "sessions": args.sessions,
                "messages": args.sessions * args.messages,
                "source_db_bytes": source_database(home).stat().st_size,
            }
        ),
        flush=True,
    )
    env = private_environment(root / "destination")
    data = {
        "id": "b" * 32,
        "project": str(project),
        "home": str(home),
        "container": "c" * 64,
        "opencode_version": VERSION,
    }
    workspace = root / "registry" / "workspace.json"
    oc_box.atomic_json(workspace, data)
    (workspace.parent / "controller").mkdir()
    original_command = workspace_service.HistorySource.command
    original_run = oc_box.run
    calls = Counter()
    seconds = Counter()

    def local_process(command, environment):
        if command[:6] != ["podman", "exec", "-i", "--workdir", "/", data["container"]]:
            raise RuntimeError("Unexpected synthetic container selector")
        return oc_box.ContainerCommand(
            [sys.executable, *command[command.index("-I") :]], environment
        )

    def local_lease(scope):
        return local_process(
            original_command(scope), {"PATH": os.defpath, "LANG": "C.UTF-8"}
        )

    def host(_data, *argv):
        return opencode_compat.command(VERSION, binary, argv)

    def timed_run(command, *argv, **kwargs):
        if list(command[:2]) == [binary, "session"]:
            label = command[2]
        elif list(command[:3]) == [binary, "api", "get"]:
            label = "children"
        else:
            label = "source"
            if command[0] == "podman":
                command = local_process(command, command.environment)
                calls["isolated-reads"] += 1
        started = time.monotonic()
        try:
            return original_run(command, *argv, **kwargs)
        finally:
            seconds[label] += time.monotonic() - started
            calls[label] += 1

    with history_reader._remote_environment(str(home), version=VERSION) as (
        source_cwd,
        source_env,
    ):

        def local_exec(_data, command, **_kwargs):
            return oc_box.ContainerCommand(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    "import os,sys; os.chdir(sys.argv[1]); os.execvpe(sys.argv[2],sys.argv[2:],os.environ)",
                    str(source_cwd),
                    *command,
                ],
                source_env,
            )

        binding = oc_box._QuietSyncBinding(data["container"], (), {"PATH": os.defpath})
        binding.start_requested = binding.start_verified = True
        with (
            oc_box.workspace_lock(workspace),
            patch.dict(os.environ, env, clear=True),
            patch.object(oc_box, "CONTAINER_OPENCODE", binary),
            patch.object(oc_box, "exec_command", side_effect=local_exec),
            patch.object(oc_box, "host_command", side_effect=host),
            patch.object(oc_box, "private_host_env", return_value=env),
            patch.object(oc_box, "run", new=timed_run),
            patch.object(workspace_service.HistorySource, "command", local_lease),
        ):
            previous = None
            for attempt in ("first", "unchanged", "one-changed")[: args.passes]:
                if attempt == "one-changed":
                    with native(binary, home, VERSION) as request:
                        rename(request, "ses_bench_0000", "synthetic changed")
                calls.clear()
                seconds.clear()
                captured = io.StringIO()
                started = time.monotonic()
                token = oc_box._QUIET_SYNC_BINDING.set(binding)
                try:
                    with (
                        contextlib.redirect_stdout(captured),
                        workspace_service.history_source(
                            oc_box, data, stopped_source=True
                        ) as live,
                    ):
                        if live is None:
                            raise RuntimeError("Synthetic quiet lease was not admitted")
                        oc_box.sync_sessions(workspace, data)
                finally:
                    oc_box._QUIET_SYNC_BINDING.reset(token)
                duration = time.monotonic() - started
                checkpoints = oc_box.session_state(workspace)["synced"]
                if (
                    len(checkpoints) != args.sessions
                    or calls["source"] != args.sessions + 1
                ):
                    raise RuntimeError(
                        "Synthetic inventory/export count was incomplete"
                    )
                if attempt == "first":
                    expected = {"import": args.sessions, "export": args.sessions}
                elif attempt == "unchanged":
                    if checkpoints != previous:
                        raise RuntimeError("An unchanged checkpoint changed")
                    expected = {}
                else:
                    if {
                        ident
                        for ident in previous
                        if previous[ident] != checkpoints[ident]
                    } != {"ses_bench_0000"}:
                        raise RuntimeError("Unexpected checkpoint updated")
                    expected = dict.fromkeys(
                        ("children", "delete", "import", "export"), 1
                    )
                if not all(
                    calls[key] == expected.get(key, 0)
                    for key in ("children", "delete", "import", "export")
                ):
                    raise RuntimeError("Unexpected destination operation count")
                previous = checkpoints
                print(
                    json.dumps(
                        {
                            "attempt": attempt,
                            "seconds": round(duration, 3),
                            "calls": dict(calls),
                            "call_seconds": {
                                k: round(v, 3) for k, v in seconds.items()
                            },
                        }
                    ),
                    flush=True,
                )
                for line in captured.getvalue().splitlines():
                    if line.startswith("oc-box: History phase timing:"):
                        print(line, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", type=Path, required=True)
    parser.add_argument("--sessions", type=int, default=30)
    parser.add_argument("--messages", type=int, default=128)
    parser.add_argument("--text-size", type=int, default=8192)
    parser.add_argument("--passes", type=int, default=3, choices=(1, 2, 3))
    args = parser.parse_args()
    if not (
        1 <= args.sessions <= 400
        and 1 <= args.messages <= 1000
        and 100 <= args.text_size <= 32768
        and args.messages * args.text_size <= 16 * 1024 * 1024
        and args.sessions * args.messages * args.text_size <= 2 * 1024 * 1024 * 1024
    ):
        parser.error("Synthetic workload exceeds bounded test settings")
    binary = str(args.opencode.resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="native-sync-bench-") as directory:
        benchmark(Path(directory), binary, args)


if __name__ == "__main__":
    main()
