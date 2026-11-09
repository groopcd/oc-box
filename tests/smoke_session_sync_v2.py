"""Provider-free session-sync checks against an unmodified native OpenCode V2.

Run with TMPDIR=/tmp/opencode python3 -B tests/smoke_session_sync_v2.py
--opencode /path/to/opencode-v2. Only transport is substituted: both databases,
HTTP APIs, CLI import/export and production sync/archive code remain real.
This is an offline integration fixture, not host/Podman lifecycle acceptance.
"""

import argparse
import contextlib
import io
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from oc_box import oc_box as box
from oc_box import opencode_compat as compat
from oc_box import opencode_v2_io as bridge
from oc_box import workspace_service


class FixtureFailure(AssertionError):
    """Fixed fixture diagnostics, never native stderr or transcript contents."""


def require(condition, message):
    if not condition:
        raise FixtureFailure(message)


def bounded(command, env, cwd, timeout=30):
    process = subprocess.Popen(
        command,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        output, _errors = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
        raise FixtureFailure("Native session-sync command timed out.") from None
    require(process.returncode == 0, "Native session-sync command failed.")
    return output


def private_environment(root):
    home = root / "home"
    temporary = root / "tmp"
    home.mkdir(parents=True)
    temporary.mkdir()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "TMPDIR": str(temporary),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "OPENCODE_TEST_HOME": str(home),
        "OPENCODE_DB": "oc-box-v2.db",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_CONFIG_CONTENT": json.dumps(
            {
                "snapshots": False,
                "formatter": False,
                "update": "disable",
                "plugins": [],
                "policies": [
                    {"action": "plugin.use", "resource": "*", "effect": "deny"}
                ],
            }
        ),
    }
    for name in ("CONFIG", "DATA", "CACHE", "STATE"):
        directory = root / name.lower()
        directory.mkdir()
        env["XDG_" + name + "_HOME"] = str(directory)
    return env


def seed(request, ident, directory, *, parent=None, title=None):
    """Supported import supplies projected history without provider execution."""
    info = {
        "id": ident,
        "projectID": "global",
        "title": title or ident,
        "cost": 0,
        "tokens": {
            "input": 0,
            "output": 0,
            "reasoning": 0,
            "cache": {"read": 0, "write": 0},
        },
        "time": {"created": 1, "updated": 2},
        "location": {"directory": str(directory)},
        "metadata": {"fixture": "session-sync", "opaque": {"id": ident}},
        "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
    }
    if parent is not None:
        info["parentID"] = parent
    request(
        "POST",
        "/api/experimental/session/import",
        {
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
        },
    )
    return ident


def exported(request, ident):
    return request("GET", "/api/experimental/session/" + ident + "/export")["data"]


def rename(request, ident, title):
    request("PATCH", "/api/session/" + ident, {"title": title})


def expect_refusal(callback, message):
    try:
        callback()
    except box.BoxError:
        return
    raise FixtureFailure(message)


def unsettled_shell(source, host, root, data, path, sync, verify_archive):
    """A direct user shell is not a foreground drain, but is unsettled history."""
    ident = seed(source, "ses_fixture_shell", data["project"])
    sync()
    archive = box.archive_id(data["id"] + "-v2", ident, "ses")
    saved = path.parent / "history" / (archive + ".json")
    before = saved.read_bytes()
    host_before = exported(host, archive)
    synced = box.session_state(path)["synced"][ident]
    gate = root / "shell-release.fifo"
    os.mkfifo(gate, 0o600)
    descriptor = os.open(gate, os.O_RDWR | os.O_NONBLOCK)
    script = root / "hold-shell.py"
    script.write_text(
        "import pathlib,sys\n"
        "with pathlib.Path(sys.argv[1]).open('rb', buffering=0) as gate:\n"
        "    gate.read(1)\n"
        "print('SYNTHETIC_SHELL_COMPLETED')\n"
    )
    finished = threading.Event()
    errors = []

    def run_shell():
        try:
            source(
                "POST",
                "/api/session/" + ident + "/shell",
                {
                    "id": "msg_fixture_shell_running",
                    "command": shlex.join([sys.executable, str(script), str(gate)]),
                },
            )
        except Exception:  # noqa: BLE001 -- Never serialize native request errors.
            errors.append(True)
        finally:
            finished.set()

    worker = threading.Thread(target=run_shell, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 20
        while True:
            messages = source(
                "GET", "/api/session/" + ident + "/message?order=asc&limit=100"
            )["data"]
            if any(
                message["type"] == "shell" and message["status"] == "running"
                for message in messages
            ):
                break
            require(
                not finished.is_set() and time.monotonic() < deadline,
                "Synthetic user shell did not reach its bounded hold point.",
            )
            finished.wait(0.02)
        require(
            ident not in source("GET", "/api/session/active")["data"],
            "Synthetic user shell unexpectedly became a foreground session drain.",
        )
        require(
            not any(
                message["type"] == "shell"
                for message in exported(source, ident)["messages"]
            ),
            "Native export no longer filters the running shell fixture.",
        )
        try:
            box.export_container_session(data, ident)
        except box.HistoryPending as error:
            require(
                str(error) == bridge.UNSETTLED,
                "Native bridge did not propagate the fixed pending reason.",
            )
        else:
            raise FixtureFailure(
                "Production export accepted native filtered, unsettled history."
            )

        # Exercise the real sync loop on both sides of the pending session:
        # changing an existing earlier session is not enough to prove iteration
        # continues after the refusal. A newly discovered later session must
        # reach the real host CLI importer in this same failing sync pass.
        prior_state = box.session_state(path)
        earlier = prior_state["sessions"][0]
        earlier_checkpoint = prior_state["synced"][earlier]
        require(earlier != ident, "Pending-session ordering fixture changed.")
        rename(source, earlier, "SYNTHETIC_PROGRESS_BEFORE_PENDING")
        later = seed(source, "ses_fixture_after_pending", data["project"])
        output = io.StringIO()
        try:
            sync(output)
        except box.BoxError as error:
            require(
                str(error)
                == "1 session(s) not settled yet: "
                + bridge.UNSETTLED
                + " Their last archives are retained and other sessions were synchronized.",
                "Pending sync did not report only the fixed count and reason.",
            )
        else:
            raise FixtureFailure("Production sync accepted a running user shell.")
        require(
            "Session history synced" not in output.getvalue(),
            "Pending sync incorrectly reported complete success.",
        )
        state = box.session_state(path)
        require(
            state["sessions"].index(earlier)
            < state["sessions"].index(ident)
            < state["sessions"].index(later),
            "Native pending-session fixture did not span the refusal.",
        )
        require(
            {earlier, later} <= set(state["synced"]),
            "Pending sync skipped an independent session's success checkpoint.",
        )
        require(
            state["synced"][earlier] != earlier_checkpoint,
            "Pending sync did not advance the earlier session's success checkpoint.",
        )
        verify_archive(earlier)
        verify_archive(later)
        require(
            saved.read_bytes() == before, "Unsettled sync replaced its private archive."
        )
        require(
            box.session_state(path)["synced"][ident] == synced,
            "Unsettled sync advanced its success checkpoint.",
        )
        require(
            exported(host, archive) == host_before,
            "Unsettled sync replaced its native host archive.",
        )
    finally:
        os.write(descriptor, b"x")
        worker.join(timeout=20)
        os.close(descriptor)
    require(
        not worker.is_alive() and not errors, "Synthetic user shell failed to finish."
    )
    sync()
    verify_archive(ident)
    require(
        any(
            message["type"] == "shell"
            and message["status"] == "exited"
            and message.get("exit") == 0
            and "SYNTHETIC_SHELL_COMPLETED"
            in message.get("output", {}).get("output", "")
            for message in exported(source, ident)["messages"]
        ),
        "Completed synthetic shell history was not retained.",
    )


def exercise(binary, root):
    project = root / "project"
    nested = project / "nested"
    foreign = root / "foreign"
    nested.mkdir(parents=True)
    foreign.mkdir()
    source_env = private_environment(root / "source")
    host_env = private_environment(root / "destination")
    release = compat.version(bounded([binary, "--version"], source_env, project))
    require(
        compat.major(release) == 2, "Session-sync fixture requires native OpenCode V2."
    )
    bounded(["git", "init", "--quiet"], source_env, project)
    data = {
        "id": "b" * 32,
        "project": str(project),
        "home": source_env["HOME"],
        "container": "oc-box-synthetic-session-sync",
        "opencode_version": release,
    }
    path = root / "registry" / "workspaces" / data["id"] / "workspace.json"
    box.atomic_json(path, data)
    (path.parent / "controller").mkdir()
    source_env["OC_BOX_STATE_HOME"] = str(root / "registry")
    host_calls = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, host_env, clear=True))
        host = stack.enter_context(bridge.server(binary))
        stack.enter_context(patch.dict(os.environ, source_env, clear=True))
        source = stack.enter_context(bridge.server(binary))
        source_env["OPENCODE_PASSWORD"] = source.endpoint["password"]
        host_env["OPENCODE_PASSWORD"] = host.endpoint["password"]
        token = workspace_service.CONNECTION.set(
            {**source.endpoint, "workspace": data["id"]}
        )
        stack.callback(workspace_service.CONNECTION.reset, token)

        def local_exec(_data, command, **_kwargs):
            # Keep arbitrary production Python bridges intact, not just direct
            # native CLI commands. Only substitute the Podman process boundary.
            if command[0] == binary:
                command = compat.command(
                    release, binary, command[1:], server=source.endpoint["url"]
                )
            trampoline = "import os,sys; os.chdir(os.environ['PWD']); os.execvpe(sys.argv[1],sys.argv[1:],os.environ)"
            return box.ContainerCommand(
                [sys.executable, "-B", "-c", trampoline, *command],
                {**source_env, "PWD": str(project)},
            )

        def host_command(_data, *args):
            host_calls.append(args)
            return compat.command(release, binary, args, server=host.endpoint["url"])

        stack.enter_context(patch.object(box, "exec_command", side_effect=local_exec))
        stack.enter_context(patch.object(box, "CONTAINER_OPENCODE", binary))
        stack.enter_context(patch.object(box, "host_command", side_effect=host_command))
        stack.enter_context(
            patch.object(box, "private_host_env", return_value=host_env)
        )

        def sync(output=None):
            with contextlib.redirect_stdout(
                output if output is not None else io.StringIO()
            ):
                box.sync_sessions(path, data)

        def archive_id(ident, prefix="ses"):
            return box.archive_id(data["id"] + "-v2", ident, prefix)

        def archive_path(ident):
            return path.parent / "history" / (archive_id(ident) + ".json")

        def verify_archive(ident):
            original = exported(source, ident)
            private = box.read_json(archive_path(ident))
            native = exported(host, archive_id(ident))
            expected = json.loads(json.dumps(original["messages"]))
            for message in expected:
                message["id"] = archive_id(message["id"], "msg")
            require(
                private["messages"] == expected,
                "Private archive lost namespaced native messages.",
            )
            require(
                native["messages"] == expected,
                "Host archive lost namespaced native messages.",
            )
            relationships = {}
            info = original["info"]
            if "parentID" in info:
                relationships["parentID"] = archive_id(info["parentID"])
            if "fork" in info:
                fork = info["fork"]
                relationships["fork"] = {
                    "sessionID": archive_id(fork["sessionID"]),
                    "boundary": {
                        "type": fork["boundary"]["type"],
                        "messageID": archive_id(fork["boundary"]["messageID"], "msg"),
                    },
                }
            if relationships:
                relationships = {"version": 1, **relationships}
            for value in (private, native):
                require(
                    value["info"]["id"] == archive_id(ident),
                    "Archive session namespace changed.",
                )
                require(
                    not any(
                        key in value["info"]
                        for key in ("parentID", "fork", "permissions", "revert")
                    ),
                    "Archive retained native relationship or privilege state.",
                )
                metadata = value["info"].get("metadata", {})
                require(
                    metadata.get("oc-box.archive", {}) == relationships,
                    "Archive lost informational relationship provenance.",
                )
                require(
                    all(
                        metadata.get(key) == item
                        for key, item in info.get("metadata", {}).items()
                    ),
                    "Archive modified unrelated source metadata.",
                )
            require(
                private["info"]["title"] == "[oc-box project] " + info["title"],
                "Private archive title was not refreshed.",
            )
            require(
                native["info"]["title"] == private["info"]["title"],
                "Native host archive title was not refreshed.",
            )

        a = seed(source, "ses_fixture_a", project)
        b = seed(source, "ses_fixture_b", project)
        c = seed(source, "ses_fixture_c", project, parent=a)
        d = seed(source, "ses_fixture_d", project, parent=c)
        f = source("POST", "/api/session/" + a + "/fork", {})["data"]["id"]
        excluded = {
            seed(source, "ses_fixture_foreign", foreign),
            seed(source, "ses_fixture_nested", nested),
        }
        source(
            "POST",
            "/api/session/" + a + "/revert/stage",
            {"messageID": "msg_fixture_a", "files": False},
        )
        rename(source, d, "Newest child is not the selected root")
        graph = {a, b, c, d, f}
        inventory = box.list_container_sessions(data)
        require(
            {item["id"] for item in inventory} == graph,
            "Production inventory lost children or admitted a foreign directory.",
        )
        cli = json.loads(
            bounded(
                compat.command(
                    release,
                    binary,
                    ["session", "list", "--format", "json"],
                    server=source.endpoint["url"],
                ),
                source_env,
                project,
            )
        )
        cli_ids = {item["id"] for item in cli}
        require(
            {a, b, f} <= cli_ids and not {c, d} & cli_ids,
            "Native CLI no longer demonstrates roots-only discovery.",
        )
        parent = box.export_container_session(data, a)
        require(
            len(parent["messages"]) == 1
            and parent["messages"][0]["text"] == "SYNTHETIC_" + a,
            "Parent export unexpectedly contains child history.",
        )
        require(
            set(parent) == {"info", "messages"},
            "Parent export unexpectedly includes a child collection.",
        )
        require(
            "permissions" in parent["info"] and "revert" in parent["info"],
            "Native source did not retain the privilege/revert stripping fixture.",
        )
        fork = exported(source, f)
        require(
            "parentID" not in fork["info"] and fork["info"]["fork"]["sessionID"] == a,
            "Native fork was not an independent root.",
        )
        require(
            fork["messages"][0]["id"] != parent["messages"][0]["id"],
            "Native fork did not assign copied message identities.",
        )
        sync()
        state = box.session_state(path)
        require(
            set(state["sessions"]) == graph and state["last"] in {a, b, f},
            "Sync selected a child or omitted graph members.",
        )
        require(
            not excluded & set(state["host_sessions"]),
            "Sync archived an out-of-scope session.",
        )
        require(
            {item.stem for item in (path.parent / "history").glob("*.json")}
            == {archive_id(ident) for ident in graph},
            "Private archive set differs from the scoped graph.",
        )
        require(
            {item["id"] for item in host("GET", "/api/session?limit=100")["data"]}
            == {archive_id(ident) for ident in graph},
            "Native host archive set differs from the scoped graph.",
        )
        for ident in graph:
            verify_archive(ident)
        require(
            exported(source, c)["info"]["parentID"] == a
            and exported(source, d)["info"]["parentID"] == c,
            "Sync modified source parent relationships.",
        )
        print(
            "PASS: native V2 scoped roots, nested children, fork provenance and per-session archives.",
            flush=True,
        )

        children = {ident: exported(host, archive_id(ident)) for ident in (c, d)}
        child_digests = {ident: state["synced"][ident] for ident in (c, d)}
        rename(source, a, "Parent-only source title change")
        sync()
        verify_archive(a)
        require(
            all(
                exported(host, archive_id(ident)) == before
                for ident, before in children.items()
            ),
            "Parent replacement cascaded into unchanged host children.",
        )
        require(
            all(
                box.session_state(path)["synced"][ident] == digest
                for ident, digest in child_digests.items()
            ),
            "Parent-only update changed child sync digests.",
        )

        retained = {ident: archive_path(ident).read_bytes() for ident in (a, c, d)}
        host_retained = {ident: exported(host, archive_id(ident)) for ident in retained}
        source("DELETE", "/api/session/" + a)
        require(
            {item["id"] for item in box.list_container_sessions(data)} == {b, f},
            "Native source deletion did not remove the nested child graph.",
        )
        rename(source, b, "Independent root progresses after source deletion")
        e = seed(source, "ses_fixture_e", project)
        sync()
        current = {b, f, e}
        state = box.session_state(path)
        require(
            set(state["sessions"]) == current and state["last"] in current,
            "Deleted tracked source blocked later sync or remained selected.",
        )
        require(
            {a, c, d} <= set(state["missing"]),
            "Deleted source identities lost their recovery tracking.",
        )
        require(
            all(
                archive_path(ident).read_bytes() == before
                for ident, before in retained.items()
            ),
            "Source deletion removed or rewrote retained private history.",
        )
        require(
            all(
                exported(host, archive_id(ident)) == before
                for ident, before in host_retained.items()
            ),
            "Source deletion removed retained host history.",
        )
        for ident in current:
            verify_archive(ident)
        before = box.session_file(path).read_bytes()
        host_calls.clear()
        sync()
        require(
            box.session_file(path).read_bytes() == before,
            "Idempotent retry changed session checkpoints.",
        )
        require(
            not any("import" in args or "delete" in args for args in host_calls),
            "Idempotent retry destructively replaced host archives.",
        )
        print(
            "PASS: native V2 parent-only updates, deleted-source retention and idempotent retry.",
            flush=True,
        )

        host_child = seed(
            host,
            "ses_fixture_host_child",
            path.parent / "controller",
            parent=archive_id(b),
        )
        protected = {
            ident: exported(host, ident) for ident in (archive_id(b), host_child)
        }
        checkpoint = box.session_state(path)["synced"][b]
        rename(source, b, "Source update while host child remains attached")
        expect_refusal(
            sync, "Host replacement did not refuse an untracked native child."
        )
        require(
            all(exported(host, ident) == before for ident, before in protected.items()),
            "Refused replacement deleted or changed protected host history.",
        )
        require(
            box.session_state(path)["synced"][b] == checkpoint,
            "Refused replacement advanced its success checkpoint.",
        )
        host("DELETE", "/api/session/" + host_child)
        sync()
        verify_archive(b)
        print(
            "PASS: native V2 host-child cascade preflight retains parent and untracked child.",
            flush=True,
        )

        collision = seed(source, "ses_fixture_collision", project)
        seed(
            host,
            archive_id(collision),
            path.parent / "controller",
            title="Unmanaged conflicting host transcript",
        )
        conflict_before = exported(host, archive_id(collision))
        expect_refusal(sync, "Native CLI conflict was mistaken for a verified import.")
        require(
            exported(host, archive_id(collision)) == conflict_before,
            "Conflicting host transcript was overwritten.",
        )
        require(
            collision not in box.session_state(path)["synced"],
            "Native CLI conflict advanced its success checkpoint.",
        )
        host("DELETE", "/api/session/" + archive_id(collision))
        sync()
        verify_archive(collision)
        print(
            "PASS: native V2 CLI import conflicts require post-import transcript verification.",
            flush=True,
        )

        # Feed raw timestamp inputs to the real host importer before invoking
        # the production verifier. Importing into the source first would already
        # normalize these fields and miss the verifier's clipping behavior.
        timestamp_cases = {
            "archive_epoch": (
                {"created": 0, "updated": 0, "archived": 0},
                None,
                {"created": 0, "archived": 0},
            ),
            "idle_epoch": (
                {
                    "created": 0,
                    "updated": 0,
                    "archived": 0,
                    "idle": 0,
                    "busy": 0,
                    "viewed": 0,
                },
                "succeeded",
                {"created": 0, "archived": 0, "idle": 0, "busy": 0, "viewed": 0},
            ),
            "zero_watermarks": (
                {"created": 1, "updated": 0, "idle": 10, "busy": 0, "viewed": 0},
                "interrupted",
                {"created": 1, "idle": 10, "busy": 0, "viewed": 0},
            ),
            "clipped_to_epoch": (
                {
                    "created": 0,
                    "updated": 0,
                    "archived": 0,
                    "idle": 0,
                    "busy": 30,
                    "viewed": 40,
                },
                "failed",
                {"created": 0, "archived": 0, "idle": 0, "busy": 0, "viewed": 0},
            ),
        }
        template = exported(source, b)
        for name, (timestamps, outcome, expected_time) in timestamp_cases.items():
            transcript = json.loads(json.dumps(template))
            transcript["info"]["id"] = "ses_fixture_time_" + name
            transcript["info"]["time"] = timestamps
            transcript["info"].pop("outcome", None)
            if outcome is not None:
                transcript["info"]["outcome"] = outcome
            for index, message in enumerate(transcript["messages"]):
                message["id"] = "msg_fixture_time_" + name + "_" + str(index)
            archived = box.archive_session(transcript, data)
            target = root / ("timestamp-" + name + ".json")
            box.atomic_json(target, archived)
            bounded(
                box.host_command(data, "--pure", "import", str(target)),
                host_env,
                path.parent / "controller",
            )
            box.verify_host_archive(path, data, archived)
            imported = exported(host, archived["info"]["id"])
            require(
                all(
                    imported["info"]["time"].get(key) == expected_time.get(key)
                    for key in ("created", "archived", "idle", "busy", "viewed")
                ),
                "Native epoch-zero timestamp retention or clipping changed.",
            )
            require(
                imported["info"].get("outcome") == outcome,
                "Native epoch-zero idle outcome was not retained.",
            )
            require(
                imported["info"]["time"]["updated"] > 0,
                "Native import did not replace its supplied updated timestamp.",
            )
        print(
            "PASS: native V2 epoch-zero archive/idle/watermark retention and timestamp clipping.",
            flush=True,
        )

        unsettled_shell(source, host, root, data, path, sync, verify_archive)
        print(
            "PASS: native V2 fixed pending reasons, same-pass progress around a running shell, archive preservation and completed-history retry.",
            flush=True,
        )


def main(binary):
    binary = str(Path(binary).resolve())
    try:
        # Honor Python's normal tempfile/TMPDIR selection; never use real host
        # data or an inherited OpenCode service registration/environment.
        with tempfile.TemporaryDirectory(prefix="oc-box-session-sync-v2-") as temporary:
            exercise(binary, Path(temporary))
    except FixtureFailure:
        raise
    except Exception:  # noqa: BLE001 -- Keep private native diagnostics out of logs.
        raise FixtureFailure(
            "Native V2 session-sync fixture failed; private diagnostics were suppressed."
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
