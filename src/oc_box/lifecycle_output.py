"""Invocation-local console policy and allowlisted lifecycle journaling.

The policy applies only to launcher diagnostics. It does not capture native
output, take ownership of the terminal, or change lifecycle outcomes.
"""

import re
import sys
from collections import deque
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class _Invocation:
    command: str
    root: object
    verbose: bool
    console_level: str | None = None
    workspace: str | None = None
    pending: deque = field(default_factory=lambda: deque(maxlen=64))
    console: deque = field(default_factory=lambda: deque(maxlen=64))


_CURRENT: ContextVar[_Invocation | None] = ContextVar(
    "oc_box_lifecycle_output", default=None
)


@contextmanager
def invocation(command, root=None, *, verbose=False, console_level=None):
    token = _CURRENT.set(_Invocation(command, root, verbose, console_level))
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


def _shows_level(requested, level, levels):
    return requested == "all" or (
        requested in levels and levels.index(level) >= levels.index(requested)
    )


def feature_details():
    """Show fixed informational feature descriptions only when requested."""
    current = _CURRENT.get()
    if current is None or current.console_level is None:
        return current is not None and current.verbose
    levels = _activity_log().LEVELS
    return _shows_level(current.console_level, "info", levels)


def _activity_log():
    if __package__:
        from . import activity_log
    else:
        import activity_log
    return activity_log


def console_event(event, **fields):
    """Queue only validated metadata, without writing inside lifecycle budgets."""
    current = _CURRENT.get()
    if current is None or current.console_level is None:
        return
    activity_log = _activity_log()
    entry = activity_log.make_record(event, **fields)
    if entry is None:
        return
    # The console already belongs to this invocation. No workspace ID is
    # needed, and the remaining fields stay within the journal's closed schema.
    entry.pop("workspace", None)
    fixed_detail(activity_log.event_level(entry), activity_log.format_event(entry))


def fixed_detail(level, message):
    """Queue an internal fixed message built exclusively from safe categories."""
    current = _CURRENT.get()
    if current is not None and current.console_level is not None:
        levels = _activity_log().LEVELS
        if level in levels and _shows_level(current.console_level, level, levels):
            current.console.append(f"oc-box: {level.upper()} {message}")


def flush_console():
    current = _CURRENT.get()
    if current is None or not current.console:
        return
    try:
        with suppress(OSError, ValueError, KeyboardInterrupt):
            for line in current.console:
                print(line, file=sys.stderr)
            sys.stderr.flush()
    finally:
        current.console.clear()


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
    fields = dict(
        command=current.command,
        workspace=current.workspace,
        level=level,
        stage=stage,
        reason=reason,
        outcome=outcome,
        duration_ms=duration_ms,
        metrics=metrics,
    )
    console_event(event, **fields)
    current.pending.append((event, fields))


def flush():
    """Write queued metadata only at safe host-side reporting boundaries."""
    flush_console()
    current = _CURRENT.get()
    if current is None or not current.pending:
        return
    try:
        record_event = _activity_log().record_event
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
