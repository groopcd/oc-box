"""Staged, explicitly applied live CPU and memory ceilings for Status."""

from dataclasses import dataclass, replace
import math
import re


CPU_UNIT = 100_000
CPU_STEP = CPU_UNIT // 2
GIB = 1024**3
MAX_CPU = 1024 * CPU_UNIT
MAX_MEMORY = 1024**5

LIMIT_MESSAGES = {
    "verified": "Live limits updated and verified. Saved workspace settings were not changed.",
    "stale": "Live limits not updated: the running workspace or its limits changed. Refresh Status before staging again.",
    "busy": "Live limits not updated: the workspace is busy. Refresh Status before staging again.",
    "unsupported": "Live limits not updated: a matching local rootless Podman container and its cgroup limits could not be verified.",
    "unverified": "Live limit update could not be verified. Limits may have changed; refresh Status and check the container before staging again. No automatic retry ran.",
}


class LimitError(ValueError):
    """Fixed diagnostic only; Podman output is not presentation data."""

    def __init__(self, reason):
        self.reason = reason if reason in LIMIT_MESSAGES else "unsupported"
        super().__init__(LIMIT_MESSAGES[self.reason])


def _identity(workspace, container, started):
    return all(
        type(value) is str and re.fullmatch(pattern, value)
        for value, pattern in (
            (workspace, r"[0-9a-f]{32}"),
            (container, r"[0-9a-f]{64}"),
            (
                started,
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                r"(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})",
            ),
        )
    )


def _cpu_units(value):
    if type(value) not in (int, float) or not 0 < value <= MAX_CPU / CPU_UNIT:
        return None
    scaled = round(value * CPU_UNIT)
    if scaled > 0 and math.isclose(value, scaled / CPU_UNIT, rel_tol=0, abs_tol=1e-10):
        return scaled
    return None


def _memory_bytes(value):
    return value if type(value) is int and 0 < value <= MAX_MEMORY else None


def cpu_text(units):
    whole, fraction = divmod(units, CPU_UNIT)
    return f"{whole}.{fraction:05d}".rstrip("0").rstrip(".")


@dataclass(frozen=True)
class LimitTarget:
    workspace: str
    container: str
    started_at: str
    cpu: int | None
    memory: int | None

    def same_run(self, item):
        return (
            item.get("state") == "running"
            and item.get("workspace_id") == self.workspace
            and item.get("container_id") == self.container
            and item.get("started_at") == self.started_at
        )

    @classmethod
    def from_report(cls, item):
        if (
            not item
            or item.get("state") != "running"
            or item.get("project_status") != "present"
            or item.get("restore_incomplete")
        ):
            return None
        workspace, container, started = (
            item.get(key) for key in ("workspace_id", "container_id", "started_at")
        )
        if not _identity(workspace, container, started):
            return None
        limits = item.get("effective_limits") or {}
        return cls(
            workspace,
            container,
            started,
            _cpu_units(limits.get("cpu_cores")),
            _memory_bytes(limits.get("memory_bytes")),
        )


@dataclass(frozen=True)
class LimitDraft:
    target: LimitTarget
    cpu: int | None = None
    memory: int | None = None

    @property
    def pending(self):
        return self.cpu is not None or self.memory is not None

    def valid(self):
        if (
            type(self.target) is not LimitTarget
            or not _identity(
                self.target.workspace, self.target.container, self.target.started_at
            )
            or not self.pending
        ):
            return False
        for base, staged, step, maximum in (
            (self.target.cpu, self.cpu, CPU_STEP, MAX_CPU),
            (self.target.memory, self.memory, GIB, MAX_MEMORY),
        ):
            if base is not None and (type(base) is not int or not 0 < base <= maximum):
                return False
            if staged is not None and (
                type(staged) is not int
                or not step <= staged <= maximum
                or base is None
                or staged == base
                or (staged - base) % step
            ):
                return False
        return True

    def step(self, resource, direction):
        step, maximum = {
            "cpu": (CPU_STEP, MAX_CPU),
            "memory": (GIB, MAX_MEMORY),
        }[resource]
        base = getattr(self.target, resource)
        if base is None:
            return self
        current = getattr(self, resource)
        candidate = (base if current is None else current) + step * direction
        if candidate == base:
            return replace(self, **{resource: None})
        if not step <= candidate <= maximum:
            return self
        return replace(self, **{resource: candidate})

    def captions(self):
        return (
            "" if self.cpu is None else f"[{cpu_text(self.cpu)}]",
            "" if self.memory is None else f"[{self.memory / GIB:.8g}]",
        )
