"""Invocation-local console policy and allowlisted lifecycle journaling.

The policy applies only to launcher diagnostics. It does not capture native
output, take ownership of the terminal, or change lifecycle outcomes.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import re


@dataclass
class _Invocation:
    command: str
    root: object
    verbose: bool
    workspace: str | None = None


_CURRENT: ContextVar[_Invocation | None] = ContextVar(
    "oc_box_lifecycle_output", default=None
)


@contextmanager
def invocation(command, root=None, *, verbose=False):
    token = _CURRENT.set(_Invocation(command, root, verbose))
    try:
        yield
    finally:
        _CURRENT.reset(token)


def concise():
    current = _CURRENT.get()
    return current is not None and not current.verbose


def bind_workspace(workspace_id):
    """Associate subsequent events only with a verified registry identity."""
    current = _CURRENT.get()
    if current is not None and isinstance(workspace_id, str):
        if re.fullmatch(r"[a-f0-9]{32}", workspace_id):
            current.workspace = workspace_id


def bind_command(command):
    """Use the parsed command once available, including selector-first forms."""
    current = _CURRENT.get()
    if current is not None:
        current.command = command


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
    """Write fixed metadata only; journal failures never affect the caller."""
    current = _CURRENT.get()
    if current is None:
        return
    try:
        if __package__:
            from .activity_log import record_event
        else:
            from activity_log import record_event
        record_event(
            event,
            command=current.command,
            workspace=current.workspace,
            level=level,
            stage=stage,
            reason=reason,
            outcome=outcome,
            duration_ms=duration_ms,
            metrics=metrics,
            root=current.root,
        )
    except Exception:
        # Do not let optional diagnostics hide or replace a lifecycle result.
        pass
