#!/usr/bin/env python3
"""Stock 2.4 manual quiet-source proof; uses no real workspace or Podman."""

import argparse
import contextlib
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from oc_box import history_reader, opencode_v2_io as bridge, workspace_service
from smoke_session_sync_v2 import FixtureFailure, box, require, seed
from test_history_reader import fake_box


IDS = ("ses_quiet_root", "ses_quiet_child", "ses_quiet_second")
TABLES = (
    "automation",
    "job",
    "kv",
    "machine",
    "session_v2",
    "session_inbox",
    "session_message",
    "session_part",
    "event",
    "event_sequence",
    "migration",
)


def source_database(home):
    return home / ".local/share/opencode/oc-box-v2.db"


@contextlib.contextmanager
def native(binary, home, release):
    # This fixture initializes and reads only its disposable native store.
    with history_reader._remote_environment(str(home), version=release) as (root, env):
        cwd = Path.cwd()
        try:
            os.chdir(root)
            exit_info = {}
            with (
                patch.dict(os.environ, env, clear=True),
                bridge.server(
                    binary, exit_info=exit_info, startup_deadline=time.monotonic() + 15
                ) as request,
            ):
                yield request
            require(
                exit_info == {"code": 0, "forced": False},
                "Standalone baseline did not retire normally.",
            )
        finally:
            os.chdir(cwd)


def metadata(home):
    uri = source_database(home).as_uri() + "?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as db:
        return [
            db.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()
            for table in TABLES
        ]


def checksum(path):
    return sha256(path.read_bytes()).digest()


def native_action(local_box, action, settings, *, env=None, cwd=None):
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            (local_box.PACKAGE / "opencode_v2_io.py").read_text(),
            action,
            local_box.CONTAINER_OPENCODE,
        ],
        cwd=cwd,
        env={"PATH": os.defpath, "LANG": "C.UTF-8"} if env is None else env,
        input=json.dumps(settings),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result


def quiet_action(data, action, settings):
    source = workspace_service.quiet_history_source()
    require(source is not None, "A positive quiet lease did not expose a read client.")
    command = source.read_command(data, action)
    require(
        command[5] == data["container"] and command[-2] == "quiet-history-" + action,
        "Quiet read escaped its original container or action.",
    )
    return subprocess.run(
        [sys.executable, *command[command.index("-I") :]],
        env={"PATH": os.defpath, "LANG": "C.UTF-8"},
        input=json.dumps(settings),
        capture_output=True,
        text=True,
        timeout=30,
    )


def seed_completed(request, ident, project):
    def completed(method, route, payload):
        payload["messages"].append(
            {
                "id": "msg_quiet_unfinished",
                "type": "assistant",
                "agent": "build",
                "model": {"providerID": "fixture", "id": "fixture"},
                "time": {"created": 2, "completed": 3},
                "content": [{"type": "text", "text": "Synthetic unfinished reply"}],
            }
        )
        return request(method, route, payload)

    seed(completed, ident, project)


def run(binary, release):
    with (
        tempfile.TemporaryDirectory(prefix="oc-box-native-quiet-source-") as directory,
        socket.socket() as provider,
    ):
        root = Path(directory)
        home, project = root / "synthetic-home", root / "synthetic-project"
        home.mkdir(mode=0o700)
        project.mkdir(mode=0o700)
        plugin_marker = root / "unexpected-plugin"
        plugin = root / "forbidden.mjs"
        plugin.write_text(
            "import {writeFileSync} from 'node:fs';\n"
            f"writeFileSync({json.dumps(str(plugin_marker))}, 'unexpected');\n"
            "export default async () => ({});\n"
        )
        provider.bind(("127.0.0.1", 0))
        provider.listen(2)
        provider.setblocking(False)
        ambient = {
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "OPENCODE_CONFIG_CONTENT": json.dumps({"plugins": [plugin.as_uri()]}),
            "OPENAI_BASE_URL": f"http://127.0.0.1:{provider.getsockname()[1]}/v1",
            "OPENAI_API_KEY": "synthetic-not-a-credential",
        }
        with native(binary, home, release) as request:
            seed(request, IDS[0], project)
            seed(request, IDS[1], project, parent=IDS[0])
            seed(request, IDS[2], project)
        before = metadata(home)
        data = {
            "id": "b" * 32,
            "container": "c" * 64,
            "home": str(home),
            "project": str(project),
            "opencode_version": release,
        }
        local_box = fake_box()
        local_box.CONTAINER_OPENCODE = binary
        local_box.captured_output = box.captured_output
        local_box.container_project = box.container_project
        start = time.monotonic()
        with history_reader._remote_environment(str(home), version=release) as (
            cwd,
            env,
        ):
            result = native_action(
                local_box, "sessions", {"directory": str(project)}, env=env, cwd=cwd
            )
            require(
                result.returncode == 0 and not result.stderr,
                "Standalone inventory failed.",
            )
            expected_inventory = json.loads(result.stdout)
            require(
                {item["id"] for item in expected_inventory} == set(IDS),
                "Synthetic inventory lacks root or child.",
            )
            expected = {}
            for ident in IDS:
                result = native_action(
                    local_box, "export", {"session": ident}, env=env, cwd=cwd
                )
                require(
                    result.returncode == 0 and not result.stderr,
                    "Standalone export failed.",
                )
                exported = json.loads(result.stdout)
                require(
                    len(exported["messages"]) == 1,
                    "Synthetic history was not populated.",
                )
                expected[ident] = box.archive_session(exported, data)
        standalone_seconds = time.monotonic() - start
        command = workspace_service.HistorySource.command
        starts = []

        def local_command(source):
            original = command(source)
            require(
                original[5] == data["container"],
                "Quiet source escaped the pinned container selector.",
            )
            # Only replace Podman exec with a local process. The private
            # payload, runtime worker, pipes and lifecycle remain production.
            starts.append(original[5])
            return local_box.ContainerCommand(
                [sys.executable, *original[original.index("-I") :]], ambient
            )

        start = time.monotonic()
        lock = workspace_service.LIFECYCLE_LOCK.set(object())
        try:
            with (
                patch.object(workspace_service.HistorySource, "command", local_command),
                workspace_service.history_source(
                    local_box, data, stopped_source=True
                ) as live,
            ):
                endpoint = {key: live[key] for key in ("url", "password")}
                request = bridge.endpoint_request(endpoint)
                pid = request("GET", "/api/info")["pid"]
                result = quiet_action(
                    data,
                    "sessions",
                    {"endpoint": endpoint, "directory": str(project)},
                )
                require(
                    result.returncode == 0 and not result.stderr,
                    "Leased inventory failed.",
                )
                require(
                    json.loads(result.stdout) == expected_inventory,
                    "Leased inventory differs from standalone.",
                )
                for ident in IDS:
                    result = quiet_action(
                        data, "export", {"endpoint": endpoint, "session": ident}
                    )
                    require(
                        result.returncode == 0 and not result.stderr,
                        "Leased checked export failed.",
                    )
                    require(
                        box.archive_session(json.loads(result.stdout)["data"], data)
                        == expected[ident],
                        "Leased archive differs from standalone.",
                    )
                    require(
                        request("GET", "/api/info")["pid"] == pid,
                        "Native process changed during pass.",
                    )
            lease_seconds = time.monotonic() - start
            require(
                len(starts) == 1,
                "One quiet pass did not use exactly one bridge/native lease.",
            )
            require(
                workspace_service.live_connection() is None,
                "A private lease context survived cleanup.",
            )
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise FixtureFailure("Native process survived bridge shutdown.")
            require(
                metadata(home) == before,
                "Read-only history changed persisted execution or transcript metadata.",
            )

            pending = root / "pending"
            shutil.copytree(home, pending)
            pending_id = "ses_quiet_pending"
            with native(binary, pending, release) as request:
                seed_completed(request, pending_id, project)
            # Native import drops unfinished messages. Change only the
            # completion marker in the genuine, stopped synthetic projection.
            with (
                contextlib.closing(sqlite3.connect(source_database(pending))) as store,
                store,
            ):
                updated = store.execute(
                    "UPDATE session_message SET data=json_remove(data,'$.time.completed') "
                    "WHERE session_id=? AND id=? AND type='assistant'",
                    (pending_id, "msg_quiet_unfinished"),
                )
                require(
                    updated.rowcount == 1, "The synthetic assistant was not projected."
                )
            pending_before = metadata(pending)
            pending_data = {**data, "home": str(pending)}
            with (
                patch.object(workspace_service.HistorySource, "command", local_command),
                workspace_service.history_source(
                    local_box, pending_data, stopped_source=True
                ) as live,
            ):
                endpoint = {key: live[key] for key in ("url", "password")}
                result = quiet_action(
                    pending_data,
                    "export",
                    {"endpoint": endpoint, "session": pending_id},
                )
                require(
                    result.returncode == 2
                    and not result.stdout
                    and bridge.fixed_message(result.stderr) == bridge.UNSETTLED,
                    "Unfinished native history did not remain a fixed pending export refusal.",
                )
                result = quiet_action(
                    pending_data, "export", {"endpoint": endpoint, "session": IDS[0]}
                )
                require(
                    result.returncode == 0
                    and box.archive_session(json.loads(result.stdout)["data"], data)
                    == expected[IDS[0]],
                    "Pending history prevented progress on an unrelated settled session.",
                )
            require(
                metadata(pending) == pending_before,
                "The pending transcript changed during the quiet pass.",
            )

            statements = (
                (
                    "jobs-present",
                    "INSERT INTO job (notification_id,id,session_id,kind,status,recovery,"
                    "time_created,time_updated) VALUES ('msg_job','job_quiet','ses_quiet_root',"
                    '\'shell\',\'completed\',\'{"kind":"shell","sessionID":"ses_quiet_root",'
                    '"shellID":"job_quiet","command":"true"}\',1,1)',
                ),
                (
                    "queue-metadata",
                    "INSERT INTO session_inbox VALUES ('q','ses_quiet_root','user',"
                    "'{}','queue',1,1)",
                ),
                ("queued-work", None),
                ("execution-present", "UPDATE session_v2 SET time_busy=3,time_idle=2"),
                ("video-state", "INSERT INTO kv VALUES ('video:queued','{}',1,1)"),
                (
                    "machine-state",
                    "INSERT INTO machine (id,key,time_created) VALUES ('m',NULL,1)",
                ),
                (
                    "schedule-present",
                    "INSERT INTO automation (id,project_id,directory,name,trigger,tz,"
                    "action,status,time_created,time_updated) VALUES ('a','p','/p','a','{}','UTC',"
                    "'{}','paused',1,1)",
                ),
            )
            clean_fallbacks = 0
            refusals = 0
            for index, (category, statement) in enumerate(statements):
                refused = root / f"refused-{index}"
                shutil.copytree(home, refused)
                database = source_database(refused)
                if statement is None:
                    with native(binary, refused, release) as request:
                        admitted = request(
                            "POST",
                            "/api/session/" + IDS[0] + "/prompt",
                            {
                                "id": "msg_quiet_queued",
                                "text": "synthetic never executed",
                                "delivery": "queue",
                                "resume": False,
                            },
                        )
                        require(
                            admitted["data"]["delivery"] == "queue"
                            and request("GET", "/api/session/active")["data"] == {},
                            "Native queue fixture was not admitted idle.",
                        )
                else:
                    with contextlib.closing(sqlite3.connect(database)) as store, store:
                        store.execute(statement)
                refused_before = checksum(database)
                log = database.parent / "log/opencode.log"
                log_before = checksum(log)
                try:
                    with (
                        patch.object(
                            workspace_service.HistorySource, "command", local_command
                        ),
                        workspace_service.history_source(
                            local_box,
                            {**data, "home": str(refused)},
                            stopped_source=True,
                        ) as connection,
                    ):
                        require(
                            category in bridge.HISTORY_SOURCE_24_FALLBACKS
                            and connection is None
                            and workspace_service.live_connection() is None,
                            "A rejected store reached native admission or unexpected fallback.",
                        )
                        clean_fallbacks += 1
                except local_box.BoxError as error:
                    require(
                        category not in bridge.HISTORY_SOURCE_24_FALLBACKS
                        and error.history_source_category == category,
                        "Unexpected fixed refusal category.",
                    )
                    refusals += 1
                require(
                    checksum(database) == refused_before
                    and checksum(log) == log_before,
                    "A refused source changed the store or started native logging.",
                )
            missing = root / "missing"
            missing.mkdir()
            with (
                patch.object(workspace_service.HistorySource, "command", local_command),
                workspace_service.history_source(
                    local_box, {**data, "home": str(missing)}, stopped_source=True
                ) as connection,
            ):
                require(
                    connection is None and not list(missing.iterdir()),
                    "A missing database was initialized or did not opt out cleanly.",
                )
            clean_fallbacks += 1
            other_release = next(iter(bridge.HISTORY_SOURCE_24 - {release}))
            try:
                with (
                    patch.object(
                        workspace_service.HistorySource, "command", local_command
                    ),
                    workspace_service.history_source(
                        local_box,
                        {
                            **data,
                            "home": str(missing),
                            "opencode_version": other_release,
                        },
                        stopped_source=True,
                    ),
                ):
                    raise FixtureFailure(
                        "A patch identity mismatch reached history admission."
                    )
            except local_box.BoxError as error:
                require(
                    error.history_source_category == "unsupported-version",
                    "Unexpected fixed patch mismatch category.",
                )
        finally:
            workspace_service.LIFECYCLE_LOCK.reset(lock)

        try:
            unexpected, _ = provider.accept()
        except BlockingIOError:
            pass
        else:
            unexpected.close()
            raise FixtureFailure("Quiet history contacted the synthetic provider.")
        require(
            not plugin_marker.exists(), "Quiet history discovered an ambient plugin."
        )
        return {
            "release": release,
            "archives_equal": len(IDS),
            "native_starts": 1,
            "standalone_starts": len(IDS) + 1,
            "guard_refusals": refusals,
            "clean_preflight_fallbacks": clean_fallbacks,
            "lease_seconds": round(lease_seconds, 3),
            "standalone_seconds": round(standalone_seconds, 3),
            "native_reaped": True,
            "pending_export_refused_without_mutation": True,
            "patch_mismatch_refused": True,
            "provider_connections": 0,
            "plugin_executed": False,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True, type=Path)
    parser.add_argument(
        "--release", required=True, choices=sorted(bridge.HISTORY_SOURCE_24)
    )
    args = parser.parse_args()
    try:
        print(
            json.dumps(run(str(args.opencode.absolute()), args.release), sort_keys=True)
        )
    except FixtureFailure as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print(
            "Native quiet-source proof failed; no private diagnostics emitted.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
