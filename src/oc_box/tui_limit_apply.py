"""One bounded local Podman update for a reviewed running container."""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import time

if __package__:
    from . import (
        entrypoint_migration,
        history_overlap_domains,
        opencode_compat,
        runtime_safeguard,
        status_report,
        tui_limits,
        tui_storage,
        workspace_control,
        workspace_podman,
    )
else:
    import entrypoint_migration
    import history_overlap_domains
    import opencode_compat
    import runtime_safeguard
    import status_report
    import tui_limits
    import tui_storage
    import workspace_control
    import workspace_podman


_SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
_SCOPE_KEYS = ("graphRoot", "runRoot", "volumePath")
_CGROUP_ROOT = "/sys/fs/cgroup"
_META_BYTES = 1024 * 1024
_SECONDS = 24


def _require(condition, reason="unsupported"):
    if not condition:
        raise tui_limits.LimitError(reason)


def _dir_stamp(info):
    return info.st_dev, info.st_ino


class _Engine:
    def __init__(self, executable, environment, deadline):
        self.executable, self.environment, self.deadline = (
            executable,
            environment,
            deadline,
        )
        self.flags = ("--remote=false",)
        self.roots = ()

    def _run(self, args, limit):
        self.executable.recheck()
        for root in self.roots:
            root.recheck()
        code, result = workspace_podman._capture_pinned(
            self.executable,
            (*self.flags, *args),
            self.environment,
            self.deadline,
            limit,
            detach_terminal=True,
        )
        self.executable.recheck()
        _require(code == 0)
        return result

    def scope(self):
        info = workspace_podman._json(
            self._run(("info", "--format", "json"), _META_BYTES)
        )
        host, store = info["host"], info["store"]
        _require(
            host["serviceIsRemote"] is False
            and host["security"]["rootless"] is True
            and host["cgroupVersion"] == "v2"
            and store.get("transientStore", False) is False
            and store.get("graphDriverName") in ("overlay", "vfs", "btrfs")
        )
        return (
            *[store[key] for key in _SCOPE_KEYS],
            store["graphDriverName"],
            store.get("graphOptions"),
            store.get("configFile"),
        )

    def inspect(self, kind, selector):
        rows = workspace_podman._json(
            self._run((kind, "inspect", "--", selector), _META_BYTES)
        )
        _require(type(rows) is list and len(rows) == 1 and type(rows[0]) is dict)
        return rows[0]

    def update(self, ident, flags):
        # Only the validated caller supplies these flags and an exact full ID.
        self._run(("update", *flags, "--", ident), 4096)


@contextmanager
def _engine(box, deadline):
    policy = workspace_podman
    uid = policy._host_uid(os.environ)
    _require(
        not (os.environ.keys() & policy._ROUTING_NAMES)
        and not any(key.startswith(policy._ROUTING_PREFIXES) for key in os.environ)
    )
    environment = box.maintenance_host_env()
    path = shutil.which("podman", path=environment.get("PATH", _SYSTEM_PATH))
    _require(path is not None)
    path = str(Path(path).resolve(strict=True))
    environment = {**environment, "PATH": _SYSTEM_PATH, "LC_ALL": "C"}
    with ExitStack() as stack:
        binary = policy._pin(stack, path, uid, owner=0, directory=False)
        _require(
            binary.stamp[4] & 0o111
            and not binary.stamp[4] & 0o6000
            and policy._read_pin(binary, 4) == b"\x7fELF"
        )
        engine = _Engine(binary, environment, deadline)
        scope = engine.scope()
        engine.roots = tuple(
            policy._pin(stack, entry, uid, owner=uid, directory=True)
            for entry in scope[:3]
        )
        # Passing --root/--storage-driver would clear native graph driver
        # options. Keep the same effective config throughout this operation.
        _require(engine.scope() == scope)
        yield engine, scope


def _registration(box, target):
    record = tui_storage.read_workspace_action(box, target.workspace)
    _require(
        record.project_condition == "present"
        and record.data.get("ready") is True
        and not record.data.get("restore_incomplete")
        and not entrypoint_migration.in_progress(record.data),
        "stale",
    )
    guard = runtime_safeguard.load(box, record.row.record_path)
    if guard is not None:
        _require(
            guard.container == target.container
            and guard.volume == record.row.home_volume,
            "stale",
        )
    return record, guard


@contextmanager
def _serialize(record):
    try:
        with workspace_control.live_limit_gate(
            record.row.record_path, timeout=0
        ) as check:
            yield check
    except workspace_control.ControlError as error:
        raise tui_limits.LimitError(
            "busy" if error.category == "control-timeout" else "unsupported"
        ) from None


@contextmanager
def _admission(record):
    if opencode_compat.major(record.data.get("opencode_version", "1.0.0")) != 2:
        yield lambda: None
        return
    path = record.row.record_path
    try:
        token, uncertain = workspace_control.disposal_observation(path)
        _require(not uncertain, "busy")
        with workspace_control.admission(path, timeout=0):

            def recheck():
                _require(workspace_control.unchanged(path, token), "stale")

            recheck()
            yield recheck
    except workspace_control.ControlError:
        raise tui_limits.LimitError("busy") from None


def _read(fd, filename, *, optional=False, limit=4096):
    try:
        source = os.open(
            filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
        )
    except FileNotFoundError:
        if optional:
            return None
        raise
    with os.fdopen(source, "rb") as stream:
        raw = stream.read(limit + 1)
    _require(0 < len(raw) <= limit and raw.endswith(b"\n"))
    return raw[:-1].decode("ascii")


def _positive_number(raw, *, maximum=2**63 - 1):
    _require(re.fullmatch(r"[0-9]{1,19}", raw) is not None)
    number = int(raw)
    _require(number <= maximum)
    return number


def _ceiling(fd, name):
    value = _read(fd, name, optional=True)
    if value is None:
        return None
    return -1 if value == "max" else _positive_number(value)


@dataclass(frozen=True)
class _Live:
    identity: tuple
    cpu: tuple | None
    memory: int | None
    swap: int | None

    def cpu_units(self):
        if self.cpu is None:
            return None
        quota, period = self.cpu
        return tui_limits._cpu_units(quota / period)


def _cgroup(pid, path, ident):
    _require(
        type(path) is str
        and path.startswith("/")
        and any(
            part in (f"libpod-{ident}", f"libpod-{ident}.scope", ident)
            for part in path.split("/")
        )
    )
    start = history_overlap_domains.observe_process_start(pid)
    with workspace_podman._path_fd(
        f"{_CGROUP_ROOT}{path}", os.geteuid(), directory=True
    ) as (directory, info):

        def recheck():
            proc = os.open(
                f"/proc/{pid}",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            try:
                _require(_read(proc, "cgroup", limit=8192) == f"0::{path}", "stale")
            finally:
                os.close(proc)
            _require(
                history_overlap_domains.observe_process_start(pid) == start, "stale"
            )

        recheck()
        cpu = _read(directory, "cpu.max", optional=True)
        if cpu is not None:
            quota, period = cpu.split(" ")
            period = _positive_number(period)
            _require(period > 0)
            cpu = (-1 if quota == "max" else _positive_number(quota), period)
        result = _Live(
            (pid, start, path, _dir_stamp(info)),
            cpu,
            _ceiling(directory, "memory.max"),
            _ceiling(directory, "memory.swap.max"),
        )
        recheck()
        return result


def _configured_limits(live, limits):
    """Podman re-applies the full resource spec, not just flags in this call."""
    quota, period, memory, combined = (
        limits.get(key) for key in ("CpuQuota", "CpuPeriod", "Memory", "MemorySwap")
    )
    _require(
        all(type(value) is int for value in (quota, period, memory, combined))
        and live.cpu is not None
        and live.memory is not None
        and live.swap is not None
    )
    observed_cpu = status_report.observed_cpu_limit(limits)
    if quota > 0:
        cpu = tui_limits._cpu_units(observed_cpu)
        _require(
            cpu is not None and live.cpu == (quota, period) and live.cpu_units() == cpu,
            "stale",
        )
    else:
        _require(
            quota in (0, -1)
            and observed_cpu == 0
            and live.cpu[0] == -1
            and period in (0, live.cpu[1]),
            "stale",
        )
        cpu = None
    _require(memory >= 0 and live.memory == (memory or -1), "stale")
    if combined in (-1, 0):
        _require(live.swap == -1, "stale")
    else:
        _require(combined >= memory and live.swap == combined - memory, "stale")
    return cpu, tui_limits._memory_bytes(memory), combined


def _snapshot(engine, box, record, target):
    data = record.data
    info = engine.inspect("container", record.row.container)
    _require(
        info["Id"] == target.container
        and info["Name"] == record.row.container
        and info["Config"]["Labels"][status_report.LABEL] == target.workspace
        and entrypoint_migration.running_replacement(info)
        and info["State"]["StartedAt"] == target.started_at,
        "stale",
    )
    mounts = info["Mounts"]
    project, home = box.container_project(data), data["home"]
    project_mounts, home_mounts = (
        [mount for mount in mounts if mount.get("Destination") == dest]
        for dest in (project, home)
    )
    _require(
        project != home
        and len(project_mounts) == len(home_mounts) == 1
        and project_mounts[0].get("Type") == "bind"
        and project_mounts[0].get("Source") == data["project"]
        and home_mounts[0].get("Type") == "volume"
        and home_mounts[0].get("Name") == record.row.home_volume,
        "stale",
    )
    volume = engine.inspect("volume", record.row.home_volume)
    _require(
        volume["Name"] == record.row.home_volume
        and volume["Driver"] == "local"
        and volume["Labels"][status_report.LABEL] == target.workspace
        and type(volume["Mountpoint"]) is str
        and volume["Mountpoint"].startswith("/")
        and volume["Mountpoint"] == home_mounts[0].get("Source"),
        "stale",
    )
    state = info["State"]
    live = _cgroup(state["Pid"], state["CgroupPath"], target.container)
    return live, *_configured_limits(live, info["HostConfig"])


def apply(box, draft, *, read_only, demo):
    """Validate, dispatch once, and verify both Podman and the live cgroup."""
    _require(read_only is False and demo is False)
    _require(type(draft) is tui_limits.LimitDraft and draft.valid())
    target = draft.target
    deadline = time.monotonic() + _SECONDS
    attempted = False
    try:
        original = _registration(box, target)
        record, _ = original
        with _engine(box, deadline) as (engine, scope):
            with _admission(record) as allowed, _serialize(record) as lock_current:
                before, cpu, memory, combined = _snapshot(engine, box, record, target)
                _require((cpu, memory) == (target.cpu, target.memory), "stale")
                flags = []
                if draft.cpu is not None:
                    flags.extend(
                        (
                            f"--cpu-period={tui_limits.CPU_UNIT}",
                            f"--cpu-quota={draft.cpu}",
                        )
                    )
                expected_combined = combined
                if draft.memory is not None:
                    _require(before.memory == memory, "stale")
                    _require(before.swap is not None)
                    # Preserve the live *swap-only* allowance. Podman's option
                    # takes RAM + swap and may otherwise set twice the new RAM.
                    if before.swap == -1:
                        _require(combined in (-1, 0))
                        expected_combined = -1
                    else:
                        _require(combined == memory + before.swap)
                        expected_combined = draft.memory + before.swap
                        _require(expected_combined < 2**53)
                    flags.extend(
                        (
                            f"--memory={draft.memory}",
                            f"--memory-swap={expected_combined}",
                        )
                    )
                _require(_registration(box, target) == original, "stale")
                _require(engine.scope() == scope)
                allowed()
                lock_current()
                # External Podman users or normal exit can still race a CLI
                # update. Never report success after losing the live generation.
                attempted = True
                engine.update(target.container, flags)
                after, new_cpu, new_memory, new_combined = _snapshot(
                    engine, box, record, target
                )
                _require(
                    after.identity == before.identity and after.swap == before.swap
                )
                if draft.cpu is None:
                    _require(new_cpu == cpu and after.cpu == before.cpu)
                else:
                    _require(new_cpu == draft.cpu and after.cpu_units() == draft.cpu)
                if draft.memory is None:
                    _require(
                        (new_memory, after.memory, new_combined)
                        == (memory, before.memory, combined)
                    )
                else:
                    _require(new_memory == after.memory == draft.memory)
                    _require(
                        new_combined == expected_combined
                        or expected_combined == -1
                        and new_combined == combined
                    )
                _require(
                    engine.scope() == scope and _registration(box, target) == original
                )
                allowed()
                lock_current()
    except Exception as error:
        reason = (
            error.reason if isinstance(error, tui_limits.LimitError) else "unsupported"
        )
        raise tui_limits.LimitError("unverified" if attempted else reason) from None
