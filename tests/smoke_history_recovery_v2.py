"""Bounded native V2 history-reader checks, never Podman lifecycle acceptance.

Run with python3 -B tests/smoke_history_recovery_v2.py --opencode /path/to/opencode.
Use unmodified 2.2.6 and 2.2.7 in separate runs. All stores/provider responses are
synthetic. Only the Podman exec boundary is replaced by local processes; reader
leases, CLI/API transport, host guards, exports/imports and verification stay real.

The original private native server is closed/reaped before Reader starts. That
LOCAL barrier does not exercise history_recovery.py's whole-container stop and
same-ID restart proof. A frozen candidate still needs the external host runner.
"""

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from smoke_session_sync_v2 import (
    BackgroundProvider,
    FixtureFailure,
    bounded,
    box,
    bridge,
    compat,
    private_environment,
    require,
    seed,
    workspace_service,
)
from oc_box import history_reader, history_recovery


CANARY = "SYNTHETIC_HISTORY_RECOVERY_AMBIENT"


def environment(root):
    """The production source reader selects HOME/.local/share, not fixture DATA."""
    env = private_environment(root)
    for kind, suffix in (
        ("CONFIG", ".config"),
        ("DATA", ".local/share"),
        ("STATE", ".local/state"),
        ("CACHE", ".cache"),
    ):
        directory = Path(env["HOME"]) / suffix
        directory.mkdir(parents=True, exist_ok=True)
        env["XDG_" + kind + "_HOME"] = str(directory)
    return env


class LocalExec:
    """Execute the unchanged remote argv/env reset locally, never call Podman."""

    def __init__(self, data, binary):
        self.data, self.binary = data, binary
        self.popen = subprocess.Popen
        self.reader_script = (box.PACKAGE / "history_reader.py").read_text()
        self.history_only = False
        self.leases = 0
        self.host_calls = []

    def __call__(self, command, *args, **kwargs):
        command = list(command)
        if command[0] != "podman":
            if self.history_only and command[0] == self.binary:
                require(
                    CANARY not in json.dumps(kwargs.get("env", {})),
                    "Recovery forwarded an ambient value to a host native command.",
                )
                self.host_calls.append(command[1:])
            return self.popen(command, *args, **kwargs)
        require(command[:3] == ["podman", "exec", "-i"], "Unexpected Podman boundary.")
        if self.history_only:
            require(
                CANARY not in json.dumps(kwargs.get("env", {})),
                "Recovery forwarded an ambient value to the source wrapper.",
            )
        index, cwd = 3, None
        # Model container create-time contamination. Production env -i/Python
        # bootstraps must discard it; do not help them by sanitizing it here.
        env = {
            "PATH": os.environ["PATH"],
            "HOME": self.data["home"],
            "PRIVATE_PROVIDER_TOKEN": CANARY,
            "OPENCODE_AUTH_CONTENT": CANARY,
        }
        while command[index] in ("--workdir", "--env"):
            option, value = command[index : index + 2]
            if option == "--workdir":
                cwd = value
            else:
                require("=" not in value, "Unexpected value-bearing exec environment.")
                env[value] = kwargs["env"][value]
            index += 2
        require(
            command[index] == self.data["container"], "Unexpected source container."
        )
        command = command[index + 1 :]
        if self.history_only:
            require(
                self.reader_script in command,
                "Source action escaped the production history reader.",
            )
            action = command[command.index(self.reader_script) + 1]
            require(action in ("lease", "command"), "Unexpected reader helper action.")
            self.leases += action == "lease"
        kwargs.update(env=env, cwd=cwd)
        return self.popen(command, *args, **kwargs)


def rows(database, table):
    require(table in ("job", "schedule"), "Unexpected fixture metadata table.")
    with contextlib.closing(
        sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)
    ) as connection:
        return connection.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall()


def add_paused_schedule(database, ident):
    """Use a real native-created schema; never fabricate the guard's schema."""
    require(rows(database, "schedule") == [], "Synthetic scheduler was not empty.")
    with contextlib.closing(sqlite3.connect(database, timeout=1)) as connection:
        connection.execute(
            'INSERT INTO schedule (id, session_id, action, "when", tz, status, '
            "next_due_at, time_created, time_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "sch_fixture_history_recovery",
                ident,
                json.dumps({"prompt": "SYNTHETIC_NEVER_EXECUTE"}),
                json.dumps({"type": "in", "in": "1 hour"}),
                "UTC",
                "paused",
                4_102_444_800_000,
                1,
                1,
            ),
        )
        connection.commit()
    return rows(database, "schedule")


def expect_schedule_refusal(callback):
    try:
        callback()
    except box.BoxError as error:
        require(
            str(error) == "History reader refused (schedule-present).",
            "Schedule guard returned a different fixed refusal.",
        )
        return
    raise FixtureFailure("History reader accepted durable scheduler state.")


def exercise(binary, root):
    project = root / "project"
    project.mkdir()
    source_env = environment(root / "source")
    host_env = environment(root / "destination")
    host_env.pop("OPENCODE_DB")  # The host guard must discover native's DEFAULT DB.
    release = compat.version(bounded([binary, "--version"], source_env, project))
    require(release in ("2.2.6", "2.2.7"), "Fixture requires native 2.2.6 or 2.2.7.")
    bounded(["git", "init", "--quiet"], source_env, project)
    data = {
        "id": "c" * 32,
        "project": str(project),
        "home": source_env["HOME"],
        "container": "e" * 64,
        "opencode_version": release,
    }
    path = root / "registry" / "workspaces" / data["id"] / "workspace.json"
    box.atomic_json(path, data)
    (path.parent / "controller").mkdir()
    host_env["OC_BOX_STATE_HOME"] = str(root / "registry")
    host_env.update(PRIVATE_PROVIDER_TOKEN=CANARY, OPENCODE_AUTH_CONTENT=CANARY)
    gate, starts, activations = (
        root / name for name in ("release", "starts", "plugins")
    )
    script = root / "hold.py"
    script.write_text(
        "import pathlib,sys,time\n"
        "gate,starts=map(pathlib.Path,sys.argv[1:])\n"
        "with starts.open('ab') as stream: stream.write(b'x')\n"
        "deadline=time.monotonic()+90\n"
        "while not gate.exists() and time.monotonic()<deadline: time.sleep(.02)\n"
        "if not gate.exists(): raise SystemExit(2)\n"
        "print('SYNTHETIC_BACKGROUND_COMPLETED')\n"
    )
    plugin = root / "plugin"
    plugin.mkdir()
    (plugin / "package.json").write_text(
        json.dumps({"type": "module", "exports": "./index.js"})
    )
    (plugin / "index.js").write_text(
        "import {appendFileSync} from 'node:fs';\n"
        "export default {id:'fixture.history-recovery',async setup(){"
        + "appendFileSync("
        + json.dumps(str(activations))
        + ",'x');}};\n"
    )
    provider = BackgroundProvider(
        shlex.join([sys.executable, str(script), str(gate), str(starts)]), project
    )
    config = json.loads(provider.config(source_env["OPENCODE_CONFIG_CONTENT"]))
    config["plugins"] = [{"package": str(plugin)}]
    config["policies"].append(
        {"action": "plugin.use", "resource": "*", "effect": "allow"}
    )
    source_env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    local = LocalExec(data, binary)
    process = None

    def stop_client():
        if process is not None and process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    def until(predicate):
        deadline = time.monotonic() + 15
        while not predicate():
            require(
                provider.errors == 0 and time.monotonic() < deadline,
                "Native recovery fixture did not reach its bounded hold point.",
            )
            time.sleep(0.05)

    with contextlib.ExitStack() as cleanup:
        cleanup.callback(provider.close)
        cleanup.callback(stop_client)
        cleanup.callback(provider.release.set)
        cleanup.callback(gate.touch)
        cleanup.enter_context(patch.object(subprocess, "Popen", new=local))
        cleanup.enter_context(patch.object(box, "CONTAINER_OPENCODE", binary))
        with (
            patch.dict(os.environ, source_env, clear=True),
            bridge.server(binary) as old,
        ):
            ident = seed(old, "ses_fixture_recovery", project)
            seed(old, "ses_fixture_independent", project)
            process = subprocess.Popen(
                [
                    binary,
                    "run",
                    "--server",
                    old.endpoint["url"],
                    "--session",
                    ident,
                    "--model",
                    "fixture/gpt-history",
                    "--agent",
                    "build",
                    "SYNTHETIC_RECOVERY",
                ],
                env={**source_env, "OPENCODE_PASSWORD": old.endpoint["password"]},
                cwd=project,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            def inventory():
                return (
                    old("GET", "/api/session/active")["data"],
                    old("GET", "/api/experimental/job")["data"],
                )

            until(
                lambda: (
                    starts.exists()
                    and inventory()[0] == {}
                    and len(inventory()[1]) == 1
                )
            )
            job = inventory()[1][0]
            require(
                job["status"] == "running"
                and job["kind"] == "shell"
                and job["sessionID"] == ident,
                "Synthetic shell was not backgrounded.",
            )
            old("POST", "/api/location/reload")
            gate.touch()
            provider.release.set()
            until(
                lambda: (
                    inventory()[0] == {}
                    and len(inventory()[1]) == 1
                    and inventory()[1][0]["status"] == "cancelled"
                )
            )
            require(
                inventory()[1][0]["id"] == job["id"],
                "Reload retained a different synthetic job.",
            )
            try:
                workspace_service.wait_history(
                    box, data, old.endpoint, timeout=3, interval=0.1
                )
            except box.HistoryWaitTimeout as error:
                require(
                    error.diagnostic.endswith(
                        "foreground=0, jobs-running=0, jobs-completed=0, "
                        "jobs-error=0, jobs-cancelled=1."
                    ),
                    "Normal history refusal lacked a complete cancelled-job observation.",
                )
            else:
                raise FixtureFailure(
                    "Normal history wait waived the retained native job."
                )
            before = bridge.checked_export(old, ident)
            require(
                provider.requests == 2, "Unexpected pre-recovery provider execution."
            )
        # This is intentionally only the native LOCAL process boundary.
        require(not old.alive(), "Original native server was not closed before Reader.")
        stop_client()
        source_db = Path(source_env["XDG_DATA_HOME"]) / "opencode/oc-box-v2.db"
        job_rows = rows(source_db, "job")
        require(len(job_rows) == 1, "Original durable job row was not retained.")
        require(activations.exists(), "Synthetic plugin canary was never activated.")
        executions = (provider.requests, starts.read_bytes(), activations.read_bytes())
        # Persist an already-proven plugin/provider config. Reader and host CLI
        # must not load it, even when their source/destination data are real.
        for env in (source_env, host_env):
            saved = Path(env["XDG_CONFIG_HOME"]) / "opencode/opencode.json"
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_text(json.dumps(config))
        local.history_only = True
        deadline = time.monotonic() + 120

        def remaining():
            budget = deadline - time.monotonic()
            require(
                budget > 0, "Native recovery fixture exhausted its snapshot budget."
            )
            return budget

        def host_command(_data, *arguments):
            return compat.command(release, binary, arguments)

        stop_token = history_recovery.workspace_control.capture(path)
        snapshot_source = history_recovery.Source(data["container"], stop_token, ())
        with (
            patch.dict(os.environ, host_env, clear=True),
            patch.object(box, "host_command", side_effect=host_command),
            box.workspace_lock(path),
            history_recovery.budget(120),
            history_recovery.commands(path, snapshot_source),
            history_reader.Reader(box, data, remaining=remaining) as reader,
        ):
            require(
                history_reader.current() is reader,
                "Recovery reader context was not installed.",
            )
            require(
                workspace_service.live_connection() == reader.connection,
                "Recovery endpoint was not shared.",
            )
            fresh = bridge.endpoint_request(reader.connection)
            pid = fresh("GET", "/api/info")["pid"]
            hydrated = fresh("GET", "/api/experimental/job")["data"]
            require(
                len(hydrated) == 1
                and hydrated[0]["id"] == job["id"]
                and hydrated[0]["status"] == "running",
                "Fresh reader did not expose the durable historical job.",
            )

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
                    require(
                        error.recovery_eligible,
                        "Rehydrated shell metadata lost post-stop snapshot eligibility.",
                    )
                else:
                    raise FixtureFailure(
                        "Normal readiness waived a rehydrated historical job."
                    )
            reader.assert_idle()
            current = bridge.checked_export(fresh, ident)
            require(
                current["messages"] == before["messages"],
                "Recovery invented source history.",
            )
            require(
                not any(
                    message.get("metadata", {}).get("jobID") == job["id"]
                    for message in current["messages"]
                ),
                "Recovery invented a background completion notice.",
            )
            try:
                reader.command([sys.executable, "-c", "raise SystemExit(0)"])
            except box.BoxError:
                pass
            else:
                raise FixtureFailure(
                    "Reader admitted an arbitrary non-history command."
                )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                box.sync_sessions(path, data, announce=False)
            require(
                "Session history synced" not in output.getvalue(),
                "Recovery printed ordinary sync success.",
            )
            state = box.session_state(path)
            require(
                set(state["synced"]) == {ident, "ses_fixture_independent"},
                "Recovery lost per-session progress.",
            )
            for source_id in state["sessions"]:
                expected = box.archive_session(
                    bridge.checked_export(fresh, source_id), data
                )
                saved = path.parent / "history" / (expected["info"]["id"] + ".json")
                require(
                    box.read_json(saved) == expected,
                    "Recovery archive differs from checked source export.",
                )
                native = box.run(
                    box.host_command(data, "--pure", "export", expected["info"]["id"]),
                    env=box.private_host_env(path, data),
                    cwd=path.parent / "controller",
                    capture=True,
                    private=True,
                    timeout=min(remaining(), 30),
                )
                require(
                    json.loads(native.stdout)["messages"] == expected["messages"],
                    "Native host archive lost transcript content.",
                )
            require(
                local.leases == 1 and fresh("GET", "/api/info")["pid"] == pid,
                "Recovery opened more than one source service.",
            )
            require(
                fresh("GET", "/api/session/active")["data"] == {},
                "Recovery started foreground work.",
            )
            require(
                rows(source_db, "job") == job_rows,
                "Recovery removed or rewrote the original job row.",
            )
            discovery = box.run(
                box.host_command(data, "debug", "paths", "db"),
                env=box.private_host_env(path, data),
                capture=True,
                private=True,
                timeout=min(remaining(), 15),
            )
            host_db = Path(discovery.stdout.removesuffix("\n"))
            require(
                host_db.is_relative_to(Path(host_env["XDG_DATA_HOME"])),
                "Destination escaped its private store.",
            )
            scheduled = add_paused_schedule(host_db, state["host_sessions"][ident])
            calls_before = len(local.host_calls)
            expect_schedule_refusal(lambda: box.private_host_env(path, data))
            require(
                local.host_calls[calls_before:]
                and all(
                    call == ["--version"] or call[:3] == ["debug", "paths", "db"]
                    for call in local.host_calls[calls_before:]
                ),
                "Destination guard ran native projection before refusing schedules.",
            )
            require(
                rows(host_db, "schedule") == scheduled,
                "Destination guard deleted scheduler state.",
            )
        require(history_reader.current() is None, "Reader context survived shutdown.")
        scheduled = add_paused_schedule(source_db, ident)
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
                raise FixtureFailure("Source schedule guard admitted a native reader.")

        with (
            patch.dict(os.environ, host_env, clear=True),
            patch.object(box, "CONTAINER_OPENCODE", str(observed_binary)),
        ):
            expect_schedule_refusal(refused_reader)
        require(
            not marker.exists(), "Source guard booted native before schedule refusal."
        )
        require(
            rows(source_db, "schedule") == scheduled,
            "Source guard deleted scheduler state.",
        )
        require(
            rows(source_db, "job") == job_rows, "Source guard changed retained jobs."
        )
        require(
            history_reader.current() is None
            and workspace_service.live_connection() is None,
            "Source refusal retained a reader or endpoint context.",
        )
        require(
            (provider.requests, starts.read_bytes(), activations.read_bytes())
            == executions
            and provider.errors == 0,
            "Reader or a schedule guard executed a provider, shell or user plugin.",
        )
    print(
        f"PASS: native {release} local-barrier retained-job recovery, one reader, "
        "real archive/import verification and source/destination schedule refusals.",
        flush=True,
    )
    print(
        "SCOPE: local native/offline only; whole-container stop/restart and "
        "external host acceptance are not exercised.",
        flush=True,
    )


def main(binary, temp_dir=None):
    previous = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(
            prefix="oc-box-history-recovery-v2-", dir=temp_dir
        ) as temporary:
            exercise(str(Path(binary).resolve()), Path(temporary))
    except FixtureFailure:
        raise
    except Exception:
        raise FixtureFailure(
            "Native history-recovery fixture failed; private diagnostics were suppressed."
        ) from None
    finally:
        os.umask(previous)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True)
    parser.add_argument("--temp-dir")
    arguments = parser.parse_args()
    try:
        main(arguments.opencode, arguments.temp_dir)
    except FixtureFailure as error:
        print("FAIL: " + str(error), file=sys.stderr)
        raise SystemExit(1) from None
