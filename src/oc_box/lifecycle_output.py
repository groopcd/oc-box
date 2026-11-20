"""Invocation-local console policy and allowlisted lifecycle journaling.

The policy applies only to launcher diagnostics. It does not capture native
output, take ownership of the terminal, or change lifecycle outcomes.
"""

import re
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class _Invocation:
    command: str
    root: object
    verbose: bool
    workspace: str | None = None
    pending: deque = field(default_factory=lambda: deque(maxlen=64))


_CURRENT: ContextVar[_Invocation | None] = ContextVar(
    "oc_box_lifecycle_output", default=None
)


@contextmanager
def invocation(command, root=None, *, verbose=False):
    token = _CURRENT.set(_Invocation(command, root, verbose))
    try:
        yield
    finally:
        try:
            flush()
        finally:
            _CURRENT.reset(token)


def concise():
    current = _CURRENT.get()
    return current is not None and not current.verbose


def bind_workspace(workspace_id):
    """Associate subsequent events only with a verified registry identity."""
    current = _CURRENT.get()
    if (
        current is not None
        and isinstance(workspace_id, str)
        and re.fullmatch(r"[a-f0-9]{32}", workspace_id)
    ):
        current.workspace = workspace_id


def bind_command(command):
    """Use the parsed command once available, including selector-first forms."""
    current = _CURRENT.get()
    if current is not None:
        current.command = command


def command_name():
    current = _CURRENT.get()
    return current.command if current is not None else None


def workspace():
    current = _CURRENT.get()
    return current.workspace if current is not None else None


def record(
    event,
    *,
    level=None,
    stage=None,
    reason=None,
    outcome=None,
    duration_ms=None,
    metrics=None,
):
    """Queue fixed metadata without disk or lock I/O inside lifecycle budgets."""
    current = _CURRENT.get()
    if current is None:
        return
    current.pending.append(
        (
            event,
            {
                "command": current.command,
                "workspace": current.workspace,
                "level": level,
                "stage": stage,
                "reason": reason,
                "outcome": outcome,
                "duration_ms": duration_ms,
                "metrics": metrics,
            },
        )
    )


def flush():
    """Write queued metadata only at safe host-side reporting boundaries."""
    current = _CURRENT.get()
    if current is None or not current.pending:
        return
    try:
        if __package__:
            from .activity_log import record_event
        else:
            from activity_log import record_event
        while current.pending:
            event, fields = current.pending.popleft()
            if not record_event(event, root=current.root, **fields):
                # Do not repeatedly wait for the same unavailable journal.
                break
    except KeyboardInterrupt:
        # Reporting runs after lifecycle exits are already determined.
        return
    except Exception:  # noqa: BLE001 - optional diagnostics must not abort callers.
        # Do not let optional diagnostics hide or replace a lifecycle result.
        return
    finally:
        current.pending.clear()
