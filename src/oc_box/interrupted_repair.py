"""Foreground review of a confirmed interrupted native subagent session."""

import contextlib
import json
import os
import re
import select
import selectors
import shlex
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

if __package__:
    from . import workspace_control, workspace_service
    from .web_named_native_profile import named_native_worker_source
else:
    import workspace_control
    import workspace_service
    from web_named_native_profile import named_native_worker_source


VERSION = "2.4.2"
SESSION = re.compile(r"ses[A-Za-z0-9_-]{1,125}")
MESSAGE = re.compile(r"msg[A-Za-z0-9_-]{1,125}")
AGENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
CONTAINER = re.compile(r"[a-f0-9]{64}")
LIMIT = 32
FLAGS = {
    "unfinished",
    "original_completed",
    "claim",
    "busy",
    "compacting",
    "archived",
    "child_revert",
    "parent_revert",
    "parent_is_child",
    "parent_archived",
    "shell_or_compaction_running",
}
COUNTS = {"child_count", "inbox_count", "job_count"}
ROW_KEYS = (
    FLAGS
    | COUNTS
    | {
        "session",
        "parent",
        "agent",
        "message",
        "tail_message",
        "last_kind",
        "job",
        "job_status",
        "job_kind",
    }
)
WORKER_LIMIT = 128 * 1024


class Unverified(ValueError):
    """Fixed refusal; worker stderr and private native data are not errors."""


class OwnerLost(Unverified):
    """End the guide instead of waiting with an invalid lease."""


def _matches(pattern, value):
    return type(value) is str and pattern.fullmatch(value) is not None


def _rows(reply):
    if type(reply) is not dict or set(reply) != {"rows"}:
        raise Unverified("Invalid interrupted-session metadata.")
    rows = reply["rows"]
    if type(rows) is not list or len(rows) > LIMIT:
        raise Unverified("Invalid interrupted-session metadata.")
    result = {}
    for row in rows:
        if (
            type(row) is not dict
            or set(row) != ROW_KEYS
            or any(not _matches(SESSION, row[key]) for key in ("session", "parent"))
            or row["session"] == row["parent"]
            or row["session"] in result
            or not _matches(AGENT, row["agent"])
            or not _matches(MESSAGE, row["message"])
            or not _matches(MESSAGE, row["tail_message"])
            or row["last_kind"]
            not in (
                "assistant",
                "user",
                "idle",
                "shell",
                "system",
                "synthetic",
                "compaction",
                "agent-switched",
                "model-switched",
                "location-switched",
                "skill",
            )
            or any(type(row[key]) is not bool for key in FLAGS)
            or any(
                type(row[key]) is not int or not 0 <= row[key] <= 10000
                for key in COUNTS
            )
            or (row["job"] is not None and not _matches(MESSAGE, row["job"]))
            or row["job_status"]
            not in (None, "running", "completed", "error", "cancelled")
            or row["job_kind"] not in (None, "subagent")
        ):
            raise Unverified("Invalid interrupted-session metadata.")
        result[row["session"]] = row
    return result


def _quiet_leaf(row):
    return not any(
        row[key]
        for key in (
            "compacting",
            "archived",
            "child_revert",
            "parent_revert",
            "parent_is_child",
            "parent_archived",
            "shell_or_compaction_running",
            "child_count",
            "inbox_count",
        )
    )


def _unfinished(row):
    return (
        row["last_kind"] == "assistant"
        and row["tail_message"] == row["message"]
        and row["unfinished"]
        and not row["original_completed"]
        and row["claim"]
    )


def _candidate(row):
    return (
        _quiet_leaf(row)
        and _unfinished(row)
        and row["job_count"] == 1
        and row["job"] is not None
        and row["job_status"] == "running"
        and row["job_kind"] == "subagent"
    )


def _no_job(row):
    return row["job_count"] == 0 and all(
        row[key] is None for key in ("job", "job_status", "job_kind")
    )


def _worker_reply(command, settings, environment):
    """Bound all three private pipes while the isolated worker runs."""
    deadline = time.monotonic() + 22
    outgoing = memoryview(json.dumps(settings).encode("utf-8"))
    process = subprocess.Popen(
        command,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    payload = bytearray()
    errors = 0
    try:
        with selectors.DefaultSelector() as io:
            for pipe in (process.stdin, process.stdout, process.stderr):
                os.set_blocking(pipe.fileno(), False)
                io.register(
                    pipe,
                    selectors.EVENT_WRITE
                    if pipe is process.stdin
                    else selectors.EVENT_READ,
                )
            while io.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not (ready := io.select(remaining)):
                    raise Unverified("The private interrupted-history check timed out.")
                for key, event in ready:
                    pipe = key.fileobj
                    if event == selectors.EVENT_WRITE:
                        outgoing = outgoing[os.write(pipe.fileno(), outgoing) :]
                        if outgoing:
                            continue
                    else:
                        chunk = os.read(pipe.fileno(), 8192)
                        if chunk:
                            if pipe is process.stdout:
                                payload.extend(chunk)
                            else:
                                errors += len(chunk)
                            if len(payload) > WORKER_LIMIT or errors > WORKER_LIMIT:
                                raise Unverified(
                                    "The private interrupted-history reply was too large."
                                )
                            continue
                    io.unregister(pipe)
                    pipe.close()
        if process.wait(timeout=max(0, deadline - time.monotonic())) != 0:
            raise Unverified("The private interrupted-history check did not complete.")
        return json.loads(payload)
    finally:
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


class Owner:
    """Reuse the ordinary local owner, pinning its container and native PID."""

    def __init__(self, box, path, data):
        self.box, self.path, self.data = box, path, data
        self.directory = box.container_project(data)
        self.environment = box.maintenance_host_env()
        info = self.inspect(data)
        self.container, self.started = info["Id"], info["State"]["StartedAt"]
        self.pinned = {**data, "container": self.container}
        self.pid = None
        self.control = None
        try:
            self.stop_token, uncertain = workspace_control.disposal_observation(path)
        except workspace_control.ControlError:
            raise Unverified(
                "The original workspace shutdown state could not be verified."
            ) from None
        if uncertain:
            raise Unverified(
                "The original workspace shutdown state could not be verified."
            )

    def inspect(self, data):
        box = self.box
        try:
            path, selected = box.find_workspace(self.data["project"])
            if path != self.path or selected != self.data:
                raise Unverified("The selected workspace registration changed.")
            info = box.inspect_owned(
                data, local_only=True, env=self.environment, private=True, timeout=5
            )
        except (box.BoxError, OSError, ValueError, subprocess.SubprocessError):
            raise Unverified(
                "The selected workspace or container could not be verified."
            ) from None
        if (
            not isinstance(info, dict)
            or not _matches(CONTAINER, info.get("Id"))
            or not isinstance(info.get("State"), dict)
            or not info["State"].get("StartedAt")
            or not box.history_recovery.verified_container_state(info, running=True)
            or box._quiet_sync_mounts(self.data, info) is None
        ):
            raise Unverified(
                "The original workspace was not verified running with its registered mounts."
            )
        return info

    def lease_ok(self):
        try:
            poll = select.poll()
            poll.register(self.control.fileno(), select.POLLIN | select.POLLRDHUP)
            if poll.poll(0) or not workspace_control.unchanged(
                self.path, self.stop_token
            ):
                raise OwnerLost(
                    "The private owner closed or a workspace stop was requested."
                )
        except (OSError, workspace_control.ControlError):
            raise OwnerLost(
                "The private owner or workspace stop state changed."
            ) from None

    def recheck(self):
        self.lease_ok()
        info = self.inspect(self.pinned)
        if info["Id"] != self.container or info["State"]["StartedAt"] != self.started:
            raise OwnerLost("The original workspace owner changed.")

    def worker(self, source, settings, *arguments):
        try:
            script = (self.box.PACKAGE / source).read_text(encoding="utf-8")
            if source == "opencode_v2_io.py":
                script = named_native_worker_source(script)
            command = [
                "podman",
                "--remote=false",
                "exec",
                "-i",
                "--workdir",
                self.directory,
                self.container,
                "/usr/bin/env",
                "-i",
                "PATH=/usr/bin:/bin",
                "python3",
                "-I",
                "-S",
                "-B",
                "-c",
                script,
                *arguments,
            ]
            return _worker_reply(command, settings, self.environment)
        except (self.box.BoxError, OSError, ValueError, subprocess.SubprocessError):
            raise Unverified(
                "The private interrupted-history check did not complete."
            ) from None

    def probe(self, connection, target=None, expect=None):
        reply = self.worker(
            "opencode_v2_io.py",
            {
                "endpoint": {key: connection[key] for key in ("url", "password")},
                "directory": self.directory,
                "target": None
                if target is None
                else {
                    key: target[key]
                    for key in ("session", "parent", "agent", "message")
                },
                "expect": expect,
            },
            "interrupted-repair-probe",
            self.box.CONTAINER_OPENCODE,
        )
        if (
            type(reply) is not dict
            or set(reply) != {"pid", "active", "check"}
            or type(reply["pid"]) is not int
            or reply["pid"] <= 1
            or (self.pid is not None and reply["pid"] != self.pid)
            or type(reply["active"]) is not list
            or len(reply["active"]) > 1024
            or any(not _matches(SESSION, item) for item in reply["active"])
            or reply["check"] not in ("active", "verified")
        ):
            raise Unverified(
                "The original private OpenCode service could not be verified."
            )
        self.pid = reply["pid"]
        return set(reply["active"]), reply["check"]

    def observe(self, target=None):
        targets = (
            None
            if target is None
            else [{key: target[key] for key in ("session", "message")}]
        )
        return _rows(
            self.worker(
                "interrupted_repair_observer.py",
                {
                    "version": VERSION,
                    "directory": self.directory,
                    "home": self.data["home"],
                    "pid": self.pid,
                    "targets": targets,
                },
            )
        )

    def check(self, connection, reviewed, stage):
        self.recheck()
        expect = "settled" if stage == "settled" else "tail"
        _, native = self.probe(connection, reviewed, expect)
        if native != "verified":
            raise Unverified(
                "The child or its parent is active; wait until both are idle."
            )
        observed = self.observe(reviewed).get(reviewed["session"])
        if (
            observed is None
            or any(
                observed[key] != reviewed[key] for key in ("parent", "agent", "message")
            )
            or not _quiet_leaf(observed)
        ):
            raise Unverified("The reviewed child, parent, or original reply changed.")
        if stage == "reviewed":
            ready = _candidate(observed) and observed["job"] == reviewed["job"]
        elif stage == "cancelled":
            ready = _no_job(observed) and _unfinished(observed)
        else:
            ready = (
                _no_job(observed)
                and observed["original_completed"]
                and not (
                    observed["unfinished"] or observed["claim"] or observed["busy"]
                )
                and observed["last_kind"] == "idle"
            )
        if not ready:
            raise Unverified("The requested repair step is not yet verified.")
        _, native = self.probe(connection, reviewed)
        self.recheck()
        if native != "verified":
            raise Unverified("The child or its parent became active.")


def _wait_for_verification(owner, connection, row, stage):
    while (
        _prompt(
            owner, "Type check to verify, or press Enter to leave this child pending: "
        )
        == "check"
    ):
        try:
            owner.check(connection, row, stage)
        except OwnerLost:
            raise
        except Unverified as exc:
            print(f"Could not verify: {exc}")
        else:
            return True
    return False


def _guide_child(owner, connection, row):
    child, parent, agent = (row[key] for key in ("session", "parent", "agent"))
    print(
        f"\nInterrupted child: {child}\nImmediate parent: {parent}\nSaved job: {row['job']}"
    )
    print(
        "Repair abandons the saved subagent job and appends one short model turn in this child."
    )
    if (
        _prompt(
            owner,
            "Type the child ID to review and open its parent, or press Enter to skip: ",
        )
        != child
    ):
        return False
    owner.check(connection, row, "reviewed")
    options = SimpleNamespace(port=0, no_open=False, web_session=parent)
    pinned_connection = {**connection, "_runtime_container": owner.container}
    with (
        workspace_service._device_engine_scope(
            owner.pinned, owner.container, owner.environment
        ),
        workspace_service.open_web_session(
            owner.box, owner.pinned, pinned_connection, options
        ),
    ):
        owner.check(connection, row, "reviewed")
        print(
            "From the opened parent, ask OpenCode to run only this Code Mode action after checking "
            "the child is still idle:\n"
            f'  await tools.opencode.session.interrupt({{sessionID: "{child}"}});\n'
            "If the tool is unavailable, skip this child or explicitly enable Full Code Mode "
            "for this workspace yourself and restore the setting afterward. "
            "This guide makes no configuration changes."
        )
        if not _wait_for_verification(owner, connection, row, "cancelled"):
            return False
        print(
            "Saved job removal verified. From the same parent send:\n"
            f'  Use the native subagent tool once with agent "{agent}" and sessionID "{child}". '
            'Reuse that exact child. Tell it: "Do not resume earlier work, use tools, '
            'or change anything. Reply with exactly: OK."'
        )
        return _wait_for_verification(owner, connection, row, "settled")


def _require_foreground():
    try:
        if (
            sys.stdin.isatty()
            and sys.stdout.isatty()
            and os.tcgetpgrp(sys.stdin.fileno()) == os.getpgrp()
        ):
            return
    except OSError:
        pass
    raise Unverified("Guided repair requires the foreground host terminal.")


def _prompt(owner, message):
    _require_foreground()
    print(message, end="", flush=True)
    ready = select.poll()
    ready.register(sys.stdin.fileno(), select.POLLIN)
    while True:
        owner.lease_ok()
        if ready.poll(250):
            break
    answer = input()
    owner.lease_ok()
    _require_foreground()
    return answer


@contextlib.contextmanager
def _close_on_suspend():
    def stopped(_signal, _frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTSTP, stopped)
    try:
        yield
    finally:
        signal.signal(signal.SIGTSTP, previous)


def guide(box, args):
    if (
        sys.platform != "linux"
        or os.geteuid() == 0
        or os.environ.get("OC_BOX_CONTAINER") == "1"
        or os.path.lexists("/.dockerenv")
        or os.path.lexists("/run/.containerenv")
        or not sys.stdin.isatty()
        or not sys.stdout.isatty()
    ):
        raise box.BoxError(
            "Run 'oc-box sync --repair-interrupted' in an interactive host terminal."
        )
    path, data = box.find_workspace(args.project, args.workspace)
    if (
        not data
        or data.get("opencode_version") != VERSION
        or data.get("ready") is not True
        or data.get("restore_incomplete")
        or data.get("devices")
        or data.get("podman_args")
        or box._stop_guard_binding(path, data) != ((), None)
        or any(
            name.startswith(("CONTAINER_", "CONTAINERS_", "PODMAN_", "STORAGE_"))
            or name == "DOCKER_HOST"
            for name in os.environ
        )
    ):
        raise box.BoxError(
            "Guided repair requires an ordinary, existing OpenCode 2.4.2 workspace on local Podman."
        )
    try:
        _require_foreground()
        if not workspace_service.controller_running(box, data, create_runtime=False):
            raise Unverified(
                "Open the same workspace normally before running guided repair."
            )
        owner = Owner(box, path, data)
        with (
            _close_on_suspend(),
            workspace_service._attach_control(
                box, path, data, create_runtime=False
            ) as attached,
        ):
            owner.control = attached.stream
            connection = attached.connection
            owner.recheck()
            if connection.get("web_mode") == workspace_service._NAMED_ONLY:
                raise Unverified(
                    "Guided repair requires a private owner with native local web access."
                )
            active, _ = owner.probe(connection)
            rows = owner.observe()
            recent, _ = owner.probe(connection)
            owner.recheck()
            candidates = [
                row
                for row in rows.values()
                if _candidate(row)
                and not (active | recent).intersection((row["session"], row["parent"]))
            ]
            if not candidates:
                print(
                    "No eligible interrupted subagent runs were verified. No sync was run."
                )
                return 0
            settled = 0
            for row in candidates:
                try:
                    verified = _guide_child(owner, connection, row)
                except (
                    Unverified,
                    box.BoxError,
                    OSError,
                    ValueError,
                    subprocess.SubprocessError,
                ):
                    print(
                        "Could not verify this repair step. Inspect the child in its parent before retrying."
                    )
                    return 1
                print(
                    "Settled: " + row["session"]
                    if verified
                    else "Still pending: " + row["session"]
                )
                settled += verified
        if settled != len(candidates):
            return 1
        command = shlex.join(("oc-box", "sync", "--workspace", data["id"]))
        print(
            "The reviewed children are settled in OpenCode. Quit the workspace normally. "
            f"If exit-time sync does not complete, run once:\n  {command}"
        )
        return 0
    except Unverified as exc:
        raise box.BoxError(str(exc)) from None
    except (EOFError, KeyboardInterrupt):
        print("Guided repair review ended. No additional sync was run.")
        return 1
