"""One fenced persisted-history pass after a verified whole-container stop.

This never settles native jobs, resumes execution, creates resources, refreshes
configuration or authorizes a live export. The original owner must already have
refused and closed. A verified container stop is the old-producer boundary.
"""

import contextlib
import contextvars
from dataclasses import dataclass
import json
import math
import re
import os
import signal
import subprocess
import sys
import threading
import time

if __package__:
    from . import history_reader, workspace_control, workspace_service
else:
    import history_reader
    import workspace_control
    import workspace_service


DEADLINE = contextvars.ContextVar("oc_box_history_recovery_deadline", default=None)
FENCE = contextvars.ContextVar("oc_box_history_snapshot_commands", default=None)
ADMITTED = contextvars.ContextVar("oc_box_history_snapshot_admitted", default=None)
TRANSITION_BUDGET = 35
SNAPSHOT_BUDGET = 180
CLEANUP_BUDGET = 30


class RecoveryError(ValueError):
    """Fixed refusal category, with no native error or resource identifiers."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(f"History recovery refused ({reason}).")


@contextlib.contextmanager
def budget(seconds):
    if (
        not isinstance(seconds, (int, float))
        or not math.isfinite(seconds)
        or seconds <= 0
    ):
        raise RecoveryError("invalid-budget")
    previous = DEADLINE.get()
    deadline = time.monotonic() + seconds
    token = DEADLINE.set(min(previous, deadline) if previous is not None else deadline)
    try:
        yield
    finally:
        DEADLINE.reset(token)


def remaining():
    deadline = DEADLINE.get()
    if deadline is None:
        return None
    value = deadline - time.monotonic()
    if value <= 0:
        raise RecoveryError("deadline")
    return value


@contextlib.contextmanager
def commands(path, source):
    if DEADLINE.get() is None or FENCE.get() is not None:
        raise RecoveryError("invalid-command-scope")
    token = FENCE.set((path, source))
    try:
        yield
    finally:
        FENCE.reset(token)


@contextlib.contextmanager
def admit():
    fence = FENCE.get()
    if fence is None:
        yield
        return
    path, source = fence
    tag = (threading.get_ident(), id(fence))
    if ADMITTED.get() == tag:
        # Forwarding observers may inspect metadata while Popen is being
        # admitted. Keep the existing lock, but still honor newly published intent.
        _unchanged(path, source)
        yield
        return
    with workspace_control.admission(path, timeout=min(5, remaining())):
        _unchanged(path, source)
        token = ADMITTED.set(tag)
        try:
            yield
        finally:
            ADMITTED.reset(token)


def run_fenced(command, **options):
    """Admit only process creation; an already admitted command may finish.

    Stop intent is published before its caller waits for admission. Holding the
    stable mutex through Popen prevents a later process from starting after an
    ordered stop, without holding stop behind an entire large-history command.
    """
    data = options.pop("input", None)
    timeout = options.pop("timeout", None)
    if data is not None:
        if options.get("stdin") is not None:
            raise ValueError("stdin and input cannot be combined")
        options["stdin"] = subprocess.PIPE
    options["start_new_session"] = True
    with admit():
        child = subprocess.Popen(command, **options)
    try:
        output, errors = child.communicate(input=data, timeout=timeout)
    except BaseException:
        if child.poll() is None:
            with contextlib.suppress(OSError):
                os.killpg(child.pid, signal.SIGKILL)
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            child.communicate(timeout=5)
        raise
    return subprocess.CompletedProcess(command, child.returncode, output, errors)


@dataclass(frozen=True)
class Source:
    container_id: str
    stop_token: str
    mount_signature: tuple


def _signature(box, data, info):
    if not isinstance(info, dict):
        raise RecoveryError("invalid-container")
    config = info.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if (
        not isinstance(config, dict)
        or not isinstance(labels, dict)
        or labels.get(box.LABEL) != data["id"]
    ):
        raise RecoveryError("ownership")
    entrypoint = config.get("Entrypoint")
    if isinstance(entrypoint, str):
        entrypoint = [entrypoint]
    if entrypoint != ["/bin/sh"] or config.get("Cmd") != ["-c", "exec sleep infinity"]:
        raise RecoveryError("nonpassive-container")
    environment = config.get("Env")
    if not isinstance(environment, list) or not all(
        isinstance(item, str) for item in environment
    ):
        raise RecoveryError("nonpassive-container")
    startup_hooks = {"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"}
    if any(
        item.split("=", 1)[0] in startup_hooks or item.startswith(("LD_", "BASH_FUNC_"))
        for item in environment
    ) or [item for item in environment if item.startswith("PATH=")] != [
        "PATH=" + history_reader.PATH
    ]:
        raise RecoveryError("automatic-container-work")
    health = config.get("Healthcheck")
    if health not in (None, {}) and (
        not isinstance(health, dict) or health.get("Test") != ["NONE"]
    ):
        raise RecoveryError("automatic-container-work")
    host = info.get("HostConfig")
    if not isinstance(host, dict):
        raise RecoveryError("invalid-container")
    if host.get("PidMode") not in ("", "private") or info.get("Pod"):
        raise RecoveryError("shared-pid-namespace")
    restart = host.get("RestartPolicy") or {}
    if not isinstance(restart, dict) or restart.get("Name", "no") not in ("", "no"):
        raise RecoveryError("automatic-container-work")
    mounts = info.get("Mounts")
    if not isinstance(mounts, list) or not all(
        isinstance(item, dict) for item in mounts
    ):
        raise RecoveryError("mounts")
    project = [
        item
        for item in mounts
        if item.get("Destination") == box.container_project(data)
    ]
    home = [item for item in mounts if item.get("Destination") == data["home"]]
    if (
        len(project) != 1
        or project[0].get("Source") != data["project"]
        or project[0].get("Type") != "bind"
        or len(home) != 1
        or home[0].get("Type") != "volume"
        or home[0].get("Name") != data["home_volume"]
        or not isinstance(home[0].get("Source"), str)
        or not home[0]["Source"].startswith("/")
    ):
        raise RecoveryError("mounts")
    return (
        project[0]["Source"],
        project[0]["Destination"],
        home[0]["Source"],
        home[0]["Destination"],
    )


def capture(box, data, info, token):
    """Use startup's existing inspection; never probe or start a healthy owner."""
    if (
        not isinstance(token, str)
        or data.get("opencode_version") not in history_reader.SUPPORTED
        or data.get("ready") is not True
        or data.get("restore_incomplete")
        or data.get("podman_args")
        or not isinstance(info, dict)
        or not isinstance(info.get("Id"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", info["Id"])
    ):
        return None
    try:
        signature = _signature(box, data, info)
    except (RecoveryError, KeyError, TypeError):
        return None
    return Source(info["Id"], token, signature)


def _command(box, arguments):
    return box.run(
        ["podman", *arguments],
        env=box.maintenance_host_env(),
        capture=True,
        private=True,
        timeout=min(20, remaining()),
    )


def _inspect(box, path, data, source, *, running):
    project = box.validate_project(data["project"])
    selected, _ = box.find_workspace(project)
    if selected != path or not box.workspace_identity.same_project(data, project):
        raise RecoveryError("project-identity")
    response = json.loads(
        _command(box, ["container", "inspect", source.container_id]).stdout
    )
    if not isinstance(response, list) or len(response) != 1:
        raise RecoveryError("invalid-container")
    info = response[0]
    if (
        not isinstance(info, dict)
        or info.get("Id") != source.container_id
        or _signature(box, data, info) != source.mount_signature
    ):
        raise RecoveryError("container-changed")
    state = info.get("State")
    if (
        not isinstance(state, dict)
        or state.get("Running") is not running
        or state.get("Paused", False) is not False
        or state.get("Restarting", False) is not False
        or type(state.get("Pid")) is not int
        or (state["Pid"] <= 0 if running else state["Pid"] != 0)
        or state.get("Status")
        not in (("running",) if running else ("exited", "stopped"))
        or (not running and state.get("ConmonPid", 0) != 0)
    ):
        raise RecoveryError("unexpected-container-state")
    return info


def _unchanged(path, source):
    if not workspace_control.unchanged(path, source.stop_token):
        raise RecoveryError("stop-requested")


def _stop(box, path, data, source):
    _command(box, ["stop", "--time", "10", source.container_id])
    _inspect(box, path, data, source, running=False)


def recover(box, path, data, source):
    """Save checked persisted history once; never convert the timeout to success.

    Caller keeps the lifecycle lock and original nonzero result. All mutations
    use the captured immutable ID, and no new container or volume is created.
    Cleanup has its own budget even if the snapshot budget has expired.
    """
    category, saved, transitioned = "unavailable", False, False
    pinned = {**data, "container": source.container_id}
    try:
        if workspace_service.live_connection() is not None:
            raise RecoveryError("old-connection")
        with workspace_control.admission(path, timeout=5), budget(TRANSITION_BUDGET):
            _unchanged(path, source)
            _inspect(box, path, data, source, running=True)
            volumes = json.loads(
                _command(box, ["volume", "inspect", data["home_volume"]]).stdout
            )
            if (
                not isinstance(volumes, list)
                or len(volumes) != 1
                or not isinstance(volumes[0], dict)
                or not isinstance(volumes[0].get("Labels"), dict)
                or volumes[0]["Labels"].get(box.LABEL) != data["id"]
                or volumes[0].get("Mountpoint") != source.mount_signature[2]
            ):
                raise RecoveryError("home-ownership")
            print(
                "oc-box: Stopping the workspace before one persisted-history snapshot retry.",
                file=sys.stderr,
            )
            _stop(box, path, data, source)
            _unchanged(path, source)
            # A failed/timed-out start has an indeterminate engine outcome. Fence
            # future recovery and successful explicit-stop claims before releasing
            # admission; a reaped host CLI is not proof that startup cannot finish.
            workspace_control.mark_uncertain(path)
            transitioned = True
            _command(box, ["start", source.container_id])
            _inspect(box, path, data, source, running=True)
            workspace_control.finish_transition(path)
            _unchanged(path, source)
        with (
            budget(SNAPSHOT_BUDGET),
            commands(path, source),
            box.loading_stage("Checking persisted history after workspace stop"),
        ):
            _unchanged(path, source)
            with history_reader.Reader(box, pinned, remaining=remaining) as reader:
                reader.assert_idle()
                _unchanged(path, source)
                box.sync_sessions(path, pinned, announce=False)
                reader.assert_idle()
                _unchanged(path, source)
            _unchanged(path, source)
            saved = True
    except RecoveryError as exc:
        category = exc.reason
    except workspace_control.ControlError:
        category = "control-state"
    except (box.BoxError, OSError, ValueError, subprocess.SubprocessError) as exc:
        reader_reason = getattr(exc, "history_reader_category", None)
        category = (
            "reader-" + reader_reason
            if isinstance(reader_reason, str)
            and reader_reason in history_reader.CATEGORIES
            else "snapshot-failed"
        )
    finally:
        if transitioned:
            try:
                with budget(CLEANUP_BUDGET):
                    _stop(box, path, data, source)
            except (box.BoxError, OSError, ValueError, subprocess.SubprocessError):
                saved, category = False, "cleanup-unverified"
    if saved:
        print(
            "oc-box: Checked persisted history snapshots were synchronized after stopping the workspace. "
            "Execution was interrupted; completion notices or final results may be missing. "
            "The original history-wait failure remains nonzero.",
            file=sys.stderr,
        )
    else:
        print(
            f"oc-box: Automatic persisted-history snapshot unavailable ({category}). "
            "Container data retained; retry with 'oc-box sync'.",
            file=sys.stderr,
        )
    return saved
