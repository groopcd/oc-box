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
    from . import (
        history_reader,
        lifecycle_output,
        startup_output,
        workspace_control,
        workspace_service,
    )
else:
    import history_reader
    import lifecycle_output
    import startup_output
    import workspace_control
    import workspace_service


DEADLINE = contextvars.ContextVar("oc_box_history_recovery_deadline", default=None)
FENCE = contextvars.ContextVar("oc_box_history_snapshot_commands", default=None)
ADMITTED = contextvars.ContextVar("oc_box_history_snapshot_admitted", default=None)
REPORT = contextvars.ContextVar("oc_box_history_report", default=None)
TRANSITION_BUDGET = 35
SNAPSHOT_BUDGET = 180
CLEANUP_BUDGET = 30
CAPTURE_REFUSALS = frozenset(
    (
        "control-unavailable",
        "unsupported-version",
        "workspace-not-ready",
        "incomplete-restore",
        "custom-podman-options",
        "invalid-container",
        "ownership",
        "nonpassive-container",
        "automatic-container-work",
        "shared-pid-namespace",
        "mounts",
    )
)
SKIP_REFUSALS = CAPTURE_REFUSALS | frozenset(
    (
        "control-state",
        "control-uncertain",
        "source-unavailable",
        "readiness-unverified",
        "insufficient-shell-observations",
        "insufficient-job-observations",
        "foreground-active",
        "non-shell-jobs",
        "jobs-empty",
        "invalid-active-response",
        "invalid-jobs-response",
        "activity-api-failed",
        "clients-not-detached",
        "owner-close-unverified",
    )
)
COMPATIBILITY_REFUSALS = frozenset(
    (
        "unsupported-version",
        "schedule-schema",
        "job-schema",
        "job-metadata",
        "job-kind",
        "legacy-jobs",
        "machine-schema",
        "invalid-active-response",
        "invalid-jobs-response",
        "active-response",
    )
)


@dataclass
class Report:
    """Reporting only: never used to authorize retries or change an exit status."""

    attempted: bool = False
    recovery_attempted: bool = False
    populated: bool | None = None
    verified: bool = False
    interrupted: bool = False
    omitted_interrupted_assistants: bool = False
    reason: str | None = None


def announce_saved():
    with contextlib.suppress(OSError, ValueError):
        startup_output.line(
            "oc-box: Checked persisted history snapshots were synchronized after stopping the workspace. "
            "Execution was interrupted; completion notices or final results may be missing. "
            "Background work is not declared complete.",
            file=sys.stderr,
        )


def compatibility_category(reason):
    if isinstance(reason, str) and reason in COMPATIBILITY_REFUSALS:
        return "unsupported-version" if reason == "unsupported-version" else "schema"
    return None


def report_compatibility_refusal(reason):
    """Actionable fixed-category guidance, never native errors or store content."""
    category = compatibility_category(reason)
    if category is None:
        return
    lifecycle_output.record(
        "history.recovery",
        level="debug",
        stage="history-recovery",
        reason=category,
        outcome="refused",
    )
    message = (
        "Automatic post-stop history recovery is unavailable: "
        f"the OpenCode compatibility contract was not recognized ({reason}). "
        "Check for an oc-box update first; upgrade OpenCode too if it is older "
        "than a supported release. If both are current, report this compatibility "
        "refusal. Keep workspace/history intact; do not reset or downgrade state "
        "to bypass the check. If shutdown history remains pending, retry with "
        "'oc-box sync'."
    )
    if lifecycle_output.concise():
        lifecycle_output.fixed_detail("warn", message)
    else:
        startup_output.line(f"oc-box: {message}", file=sys.stderr)


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


def run_admitted(command, gate, **options):
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
    with gate():
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


def run_fenced(command, **options):
    return run_admitted(command, admit, **options)


@dataclass(frozen=True)
class Source:
    container_id: str
    stop_token: str
    mount_signature: tuple


def _signature(box, data, info):
    """Validate the passive entrypoint and bind/volume signature without I/O."""
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


def passive_workspace_mounts(box, data, info):
    return _signature(box, data, info)


def capture(box, data, info, token, *, refusal=None):
    """Use startup's existing inspection; never probe or start a healthy owner."""

    def reject(reason):
        if refusal is not None:
            refusal(reason if reason in CAPTURE_REFUSALS else "invalid-container")
        return None

    if not isinstance(token, str):
        return reject("control-unavailable")
    if history_reader.snapshot_profile(data.get("opencode_version")) is None:
        return reject("unsupported-version")
    if data.get("ready") is not True:
        return reject("workspace-not-ready")
    if data.get("restore_incomplete"):
        return reject("incomplete-restore")
    if data.get("podman_args"):
        return reject("custom-podman-options")
    if (
        not isinstance(info, dict)
        or not isinstance(info.get("Id"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", info["Id"])
    ):
        return reject("invalid-container")
    try:
        signature = passive_workspace_mounts(box, data, info)
    except RecoveryError as exc:
        return reject(exc.reason)
    except (KeyError, TypeError):
        return reject("invalid-container")
    return Source(info["Id"], token, signature)


def exit_retry_eligible(box, data, failure):
    """Route one archival attempt, never authorize live reads or normal startup.

    The minor profile selects the safety contract, not a per-patch allowlist.
    On 2.4 a request that failed after the normal quiet gate must have failed
    at a positively known source boundary, not while mutating the destination.
    """
    profile = history_reader.snapshot_profile(data.get("opencode_version"))
    if profile == history_reader.SNAPSHOT_24 and (
        getattr(failure, "history_compatibility_category", None) is not None
        or getattr(failure, "history_reader_category", None) is not None
        or getattr(failure, "history_contract_refusal", False)
        or getattr(failure, "history_source_cleanup_failed", False)
    ):
        return False
    if isinstance(failure, box.HistoryWaitTimeout):
        return failure.recovery_eligible
    if profile == history_reader.SNAPSHOT_24:
        return isinstance(failure, box.HistoryPending) or (
            isinstance(failure, box.BoxError)
            and getattr(failure, "history_source_read_failed", False) is True
        )
    return (
        isinstance(failure, box.BoxError)
        and (
            isinstance(failure, box.HistoryPending)
            or getattr(failure, "history_retryable", False) is True
            or isinstance(failure.__cause__, subprocess.TimeoutExpired)
        )
        and profile == "2.3"
        and getattr(failure, "history_compatibility_category", None) is None
        and getattr(failure, "history_reader_category", None) is None
        and not getattr(failure, "history_contract_refusal", False)
        and not getattr(failure, "history_source_cleanup_failed", False)
    )


def skip_reasons(owner, timeout, *, closed=False):
    """Explain routing only; these labels never authorize or relax recovery."""
    reasons = []
    if not timeout.recovery_eligible:
        reasons.append(getattr(timeout, "recovery_reason", "readiness-unverified"))
    if getattr(owner, "_history_source", None) is None:
        reasons.append(getattr(owner, "_history_source_refusal", "source-unavailable"))
    if closed:
        clients = getattr(owner, "clients", None)
        if type(clients) is not int or clients != 0:
            reasons.append("clients-not-detached")
        result = getattr(getattr(owner, "child", None), "returncode", None)
        if type(result) is not int or result != 0:
            reasons.append("owner-close-unverified")
    return reasons


def report_skipped(reasons):
    """Print only fixed categories, never exception/metadata/job contents."""
    safe = list(
        dict.fromkeys(
            reason
            if isinstance(reason, str) and reason in SKIP_REFUSALS
            else "unavailable"
            for reason in reasons
        )
    )
    reason = (
        "cleanup-unverified"
        if {"clients-not-detached", "owner-close-unverified"}.intersection(safe)
        else next(filter(None, map(compatibility_category, safe)), "source-unavailable")
    )
    lifecycle_output.record(
        "history.recovery",
        level="debug",
        stage="history-recovery",
        reason=reason,
        outcome="unavailable",
    )
    message = (
        "Automatic persisted-history snapshot not attempted ("
        + ", ".join(safe or ["unavailable"])
        + ")."
    )
    if lifecycle_output.concise():
        lifecycle_output.fixed_detail("debug", message)
    else:
        startup_output.line(f"oc-box: {message}", file=sys.stderr)


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
        or passive_workspace_mounts(box, data, info) != source.mount_signature
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
    """Return whether checked persisted history and recovery cleanup completed.

    Caller keeps the lifecycle lock and original client result. All mutations
    use the captured immutable ID, and no new container or volume is created.
    Cleanup has its own budget even if the snapshot budget has expired.
    """
    category, saved, transitioned = "unavailable", False, False
    compatibility_reason = None
    pinned = {**data, "container": source.container_id}
    started = time.monotonic()
    report = REPORT.get()
    if report is not None:
        # A previous live pass must not supply the Reader's population result.
        report.populated = None
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
            if not lifecycle_output.concise():
                startup_output.line(
                    "oc-box: Stopping the workspace before one persisted-history snapshot retry.",
                    file=sys.stderr,
                )
            _stop(box, path, data, source)
            if report is not None:
                report.interrupted = True
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
            box.loading_stage(
                "Checking persisted history after workspace stop",
                stage="history-recovery",
            ),
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
    except (box.BoxError, OSError, ValueError, subprocess.SubprocessError) as exc:
        reader_reason = getattr(exc, "history_reader_category", None)
        if isinstance(reader_reason, str) and reader_reason in COMPATIBILITY_REFUSALS:
            compatibility_reason = reader_reason
        if getattr(exc, "history_source_cleanup_failed", False) or (
            report is not None and report.reason == "cleanup-unverified"
        ):
            category = "cleanup-unverified"
        elif isinstance(exc, RecoveryError):
            category = exc.reason
        elif isinstance(exc, workspace_control.ControlError):
            category = "control-state"
        elif isinstance(exc, box.HistoryPending):
            category = "snapshot-incomplete"
        elif (
            isinstance(reader_reason, str)
            and reader_reason in history_reader.CATEGORIES
        ):
            category = "reader-" + reader_reason
        else:
            category = "snapshot-failed"
    finally:
        if transitioned:
            try:
                with budget(CLEANUP_BUDGET):
                    _stop(box, path, data, source)
            except (box.BoxError, OSError, ValueError, subprocess.SubprocessError):
                saved, category = False, "cleanup-unverified"

    reason = None
    if not saved:
        reason = {
            "cleanup-unverified": "cleanup-unverified",
            "reader-cleanup-failed": "cleanup-unverified",
            "reader-history-cleanup": "cleanup-unverified",
            "snapshot-incomplete": "pending",
            "stop-requested": "stop-requested",
            "deadline": "timeout",
            "reader-video-state": "video-state",
            "reader-machine-state": "machine-state",
            "reader-blob-state": "blob-state",
            "reader-host-service-state": "host-service-state",
        }.get(category, compatibility_category(compatibility_reason) or "sync-failed")
    if report is not None:
        report.reason = reason
    lifecycle_output.record(
        "history.recovery",
        level="debug",
        stage="history-recovery",
        reason=reason,
        outcome="success" if saved else "failure",
        duration_ms=int(max(0, min(time.monotonic() - started, 9999)) * 1000),
    )
    # The selected category comes from internal fixed refusals, never str(exc).
    failure_message = (
        f"Automatic persisted-history snapshot unavailable ({category}). "
        "Container data retained; retry with 'oc-box sync'."
    )
    if lifecycle_output.concise():
        if not saved:
            lifecycle_output.fixed_detail("warn", failure_message)
        return saved
    if saved:
        if report is None:  # A launcher will announce only after its own final stop.
            announce_saved()
    else:
        if category == "snapshot-incomplete":
            startup_output.line(
                "oc-box: Some persisted sessions remain unsettled. "
                "Verified progress and prior archives for pending sessions are retained. "
                "Execution was interrupted; background work is not declared complete.",
                file=sys.stderr,
            )
        startup_output.line(f"oc-box: {failure_message}", file=sys.stderr)
        report_compatibility_refusal(compatibility_reason)
    return saved
