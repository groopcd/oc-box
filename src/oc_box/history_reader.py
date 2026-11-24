"""Non-executing V2 history lease, only after a verified full container stop.

The caller proves stop/restart identity, holds the lifecycle lock, supplies one
relative-budget callback, and always stops the container afterwards. This module
does not elect a service, prepare credentials, or certify live-owner quiescence.
It is also copied verbatim to ``python -c``; keep module imports stdlib-only.
"""

import contextlib
import contextvars
import json
import math
import os
from pathlib import Path
import re
import select
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

CURRENT = contextvars.ContextVar("oc_box_history_reader", default=None)
EXPORTS = contextvars.ContextVar("oc_box_history_exports", default=None)
# The profiles select storage guards, never bypass them. The subagent profiles
# are ONLY for unregistered serve --stdio, not managed-service restart continuity.
# No profile waives live readiness or establishes whole-container acceptance.
SNAPSHOT_24 = "2.4"
SUBAGENT_SNAPSHOT_PROFILES = frozenset(("2.3", SNAPSHOT_24))
_SHELL_SUBAGENT = frozenset(("shell", "subagent"))
SNAPSHOT_JOB_KINDS = {
    "2.2": frozenset(("shell",)),
    "2.3": _SHELL_SUBAGENT,
    SNAPSHOT_24: _SHELL_SUBAGENT,
}
MAX_JOB_ROWS = 10000
MAX_JOB_RECOVERY_BYTES = 65536
MAX_JOB_METADATA_BYTES = 8 * 1024 * 1024
MAX_BLOB_ENTRIES = 10000
MAX_SERVICE_ENTRIES = 4096
# The audited 2.4 releases sweep blob_* using a seven-day retention clock.
# Leave an hour of headroom beyond all Reader/cleanup deadlines.
BLOB_MAX_AGE = 7 * 24 * 3600 - 3600
MAX_CONTROL = 16384
MAX_PAYLOAD = 131072
PATH = "/opt/oc-box/runtime/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
MARKER = "oc-box-history-reader: "
GUARD_CATEGORIES = frozenset(
    (
        "database-missing",
        "database-unsafe",
        "database-invalid",
        "schedule-schema",
        "schedule-present",
        "job-schema",
        "job-metadata",
        "job-kind",
        "job-limit",
        "legacy-jobs",
        "video-state",
        "machine-schema",
        "machine-state",
        "blob-state",
        "host-service-state",
        "deadline",
        "system-config",
        "unsupported-version",
    )
)
CATEGORIES = GUARD_CATEGORIES | frozenset(
    (
        "deadline",
        "unsupported-version",
        "host-database-path",
        "host-database-probe",
        "startup-payload",
        "startup-timeout",
        "startup-failed",
        "startup-response",
        "cleanup-failed",
        "reader-ended",
        "client-command",
        "reader-reuse",
        "active-response",
        "foreground-active",
        "active-query",
        "reader-source",
        "runtime-executable-unverified",
        "history-command",
        "history-response",
        "history-cleanup",
    )
)


class Refusal(ValueError):
    """Fixed-category errors only; never include SQLite/native diagnostics."""

    def __init__(self, category):
        self.category = (
            category
            if isinstance(category, str) and category in CATEGORIES
            else "startup-failed"
        )
        super().__init__(f"History reader refused ({self.category}).")


def _boxed(box, category):
    refusal = Refusal(category)
    error = box.BoxError(str(refusal))
    error.history_reader_category = refusal.category
    return error


def current():
    return CURRENT.get()


def snapshot_profile(version):
    """Select a guarded storage contract, not an assurance of native acceptance.

    Only canonical stable versions qualify; prereleases, build suffixes and
    unknown minor/major series must not inherit an audited contract.
    """
    if not isinstance(version, str):
        return None
    if re.fullmatch(r"2\.3\.(?:0|[1-9][0-9]*)", version):
        return "2.3"
    if version in ("2.2.6", "2.2.7"):
        return "2.2"
    if re.fullmatch(r"2\.4\.(?:0|[1-9][0-9]*)", version):
        return SNAPSHOT_24
    return None


def _check_version(output, expected):
    match = re.fullmatch(r"(?:opencode\s+)?v?([12]\.\d+\.\d+)", output.strip())
    if not match or match[1] != expected or snapshot_profile(expected) is None:
        raise Refusal("unsupported-version")


def _budget(remaining, maximum):
    value = remaining()
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise Refusal("deadline")
    return min(value, maximum)


def _table_schema(connection, name, expected, category):
    """Recognize a complete audited layout, never a view or unknown extension."""
    if connection.execute(
        "SELECT type FROM sqlite_master WHERE name = ?", (name,)
    ).fetchall() != [("table",)]:
        raise Refusal(category)
    columns = {
        row[1]: row for row in connection.execute("PRAGMA table_xinfo('" + name + "')")
    }
    if set(columns) != set(expected) or any(
        columns[key][2].upper() != kind
        or bool(columns[key][5]) != primary
        or (required is not None and bool(columns[key][3]) != required)
        or columns[key][6] != 0
        for key, (kind, required, primary) in expected.items()
    ):
        raise Refusal(category)


def _check_scheduler(connection, profile):
    """Refuse before native's flag-independent scheduler/automation rewake."""
    table = "schedule"
    if profile in SUBAGENT_SNAPSHOT_PROFILES:
        layouts = connection.execute(
            "SELECT name, type FROM sqlite_master WHERE name IN ('schedule', 'automation')"
        ).fetchall()
        if len(layouts) != 1 or layouts[0][1] != "table":
            raise Refusal("schedule-schema")
        table = layouts[0][0]
        if profile == SNAPSHOT_24 and table != "automation":
            raise Refusal("schedule-schema")
        # 2.3.0's schedule and 2.3.1+'s automation are the two known contracts.
        # Empty old stores may migrate forward; missing/ambiguous/new shapes
        # never inherit a pass merely because the native version is 2.3.x.
        common = {
            "id": ("TEXT", None, True),
            "session_id": ("TEXT", False, False),
            "name": ("TEXT", table == "automation", False),
            "action": ("TEXT", True, False),
            "tz": ("TEXT", True, False),
            "status": ("TEXT", True, False),
            "next_due_at": ("INTEGER", False, False),
            "run_count": ("INTEGER", True, False),
            "failures": ("INTEGER", True, False),
            "last_message_id": ("TEXT", False, False),
            "last_outcome": ("TEXT", False, False),
            "retry_at": ("INTEGER", False, False),
            "state": ("TEXT", False, False),
            "time_created": ("INTEGER", True, False),
            "time_updated": ("INTEGER", True, False),
        }
        specific = (
            {
                "check": ("TEXT", False, False),
                "when": ("TEXT", True, False),
                "ends_at": ("INTEGER", False, False),
                "remaining_runs": ("INTEGER", False, False),
                "checks": ("INTEGER", True, False),
                "last_fired_at": ("INTEGER", False, False),
            }
            if table == "schedule"
            else {
                "project_id": ("TEXT", True, False),
                "directory": ("TEXT", True, False),
                "trigger": ("TEXT", True, False),
                "expires_at": ("INTEGER", False, False),
                "last_run_at": ("INTEGER", False, False),
                "last_output": ("TEXT", False, False),
                "event_position": ("TEXT", False, False),
            }
        )
        _table_schema(connection, table, {**common, **specific}, "schedule-schema")
    else:
        if connection.execute(
            "SELECT type FROM sqlite_master WHERE name = 'schedule'"
        ).fetchall() != [("table",)]:
            raise Refusal("schedule-schema")
        columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info('schedule')")
        }
        required = ("id", "session_id", "status", "last_message_id")
        if (
            any(
                name not in columns or columns[name][2].upper() != "TEXT"
                for name in required
            )
            or not columns["id"][5]
        ):
            raise Refusal("schedule-schema")
    if (
        connection.execute('SELECT 1 FROM "' + table + '" LIMIT 1').fetchone()
        is not None
    ):
        raise Refusal("schedule-present")
    return table


def _check_jobs(connection, deadline):
    """Validate bounded shell/subagent metadata, never outputs or transcripts.

    All known statuses remain historical metadata, not proof of completion. The
    unregistered native profile must preserve these rows, including execution
    claims/inbox state; this guard never edits, acknowledges or resumes anything.
    """

    def budget():
        if time.monotonic() >= deadline:
            raise Refusal("deadline")

    budget()
    _table_schema(
        connection,
        "job",
        {
            "notification_id": ("TEXT", None, True),
            "id": ("TEXT", True, False),
            "session_id": ("TEXT", True, False),
            "child_session_id": ("TEXT", False, False),
            "kind": ("TEXT", True, False),
            "title": ("TEXT", False, False),
            "status": ("TEXT", True, False),
            "output": ("TEXT", False, False),
            "error": ("TEXT", False, False),
            "recovery": ("TEXT", True, False),
            "time_created": ("INTEGER", True, False),
            "time_updated": ("INTEGER", True, False),
        },
        "job-schema",
    )
    _table_schema(
        connection,
        "kv",
        {
            "key": ("TEXT", None, True),
            "value": ("TEXT", True, False),
            "time_created": ("INTEGER", True, False),
            "time_updated": ("INTEGER", True, False),
        },
        "job-schema",
    )
    if connection.execute(
        "SELECT 1 FROM kv WHERE key LIKE 'job.background/%' LIMIT 1"
    ).fetchone():
        raise Refusal("legacy-jobs")

    def identity(value, prefix):
        return (
            isinstance(value, str)
            and len(value) <= 256
            and re.fullmatch(prefix + r"[A-Za-z0-9_-]+", value) is not None
        )

    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise Refusal("job-metadata")
            value[key] = item
        return value

    def outstanding(items):
        if not isinstance(items, list):
            raise Refusal("job-metadata")
        if len(items) > MAX_JOB_ROWS:
            raise Refusal("job-limit")
        for item in items:
            budget()
            if not isinstance(item, dict):
                raise Refusal("job-metadata")
            kind = item.get("kind")
            if not isinstance(kind, str) or kind not in _SHELL_SUBAGENT:
                raise Refusal("job-kind")
            keys = (
                {"kind", "sessionID", "shellID", "command"}
                if kind == "shell"
                else {"kind", "sessionID", "childSessionID", "agent", "description"}
            )
            if (
                set(item) != keys
                or any(not isinstance(value, str) for value in item.values())
                or not identity(item["sessionID"], "ses")
            ):
                raise Refusal("job-metadata")
            if kind == "shell":
                valid = bool(item["shellID"]) and len(item["shellID"]) <= 256
            else:
                valid = (
                    identity(item["childSessionID"], "ses")
                    and item["childSessionID"] != item["sessionID"]
                )
            if not valid:
                raise Refusal("job-metadata")

    total = 0
    # Bound SQL result strings BEFORE passing recovery JSON into Python. Its
    # command/description are read only to validate their types; never select the
    # job's title/output/error or any message/part/credential data.
    identities = ", ".join(
        "CASE WHEN typeof(" + name + ") = 'text' AND "
        "length(CAST(" + name + " AS BLOB)) <= 256 THEN " + name + " END"
        for name in ("notification_id", "id", "session_id", "child_session_id")
    )
    rows = connection.execute(
        "SELECT " + identities + ", child_session_id IS NULL, "
        "CASE WHEN kind IN ('shell','subagent','image') THEN kind END, "
        "CASE WHEN status IN ('running','completed','error','cancelled') THEN status END, "
        "typeof(time_created) = 'integer' AND typeof(time_updated) = 'integer', "
        "length(CAST(recovery AS BLOB)), "
        "CASE WHEN typeof(recovery) = 'text' AND "
        "length(CAST(recovery AS BLOB)) <= ? THEN recovery ELSE NULL END "
        "FROM job LIMIT ?",
        (MAX_JOB_RECOVERY_BYTES, MAX_JOB_ROWS + 1),
    )
    for count, row in enumerate(rows, 1):
        budget()
        if count > MAX_JOB_ROWS:
            raise Refusal("job-limit")
        (
            notice,
            ident,
            session,
            child,
            child_null,
            kind,
            status,
            times_valid,
            size,
            encoded,
        ) = row
        if kind not in _SHELL_SUBAGENT:
            raise Refusal("job-kind")
        if (
            not identity(notice, "msg")
            or not isinstance(ident, str)
            or not ident
            or len(ident) > 256
            or not identity(session, "ses")
            or status not in ("running", "completed", "error", "cancelled")
            or times_valid != 1
        ):
            raise Refusal("job-metadata")
        if not isinstance(encoded, str):
            raise Refusal(
                "job-limit"
                if size and size > MAX_JOB_RECOVERY_BYTES
                else "job-metadata"
            )
        total += size
        if total > MAX_JOB_METADATA_BYTES:
            raise Refusal("job-limit")
        try:
            recovery = json.loads(encoded, object_pairs_hook=pairs)
        except (ValueError, RecursionError):
            raise Refusal("job-metadata") from None
        keys = (
            {"kind", "sessionID", "shellID", "command"}
            if kind == "shell"
            else {"kind", "parentSessionID", "childSessionID", "agent", "description"}
        )
        if (
            kind == "subagent"
            and isinstance(recovery, dict)
            and "outstanding" in recovery
        ):
            keys = keys | {"outstanding"}
        if (
            not isinstance(recovery, dict)
            or set(recovery) != keys
            or any(
                not isinstance(value, str)
                for key, value in recovery.items()
                if key != "outstanding"
            )
            or recovery["kind"] != kind
        ):
            raise Refusal("job-metadata")
        if kind == "shell":
            valid = (
                child_null == 1
                and recovery["sessionID"] == session
                and recovery["shellID"] == ident
            )
        else:
            valid = (
                identity(child, "ses")
                and child != session
                and recovery["parentSessionID"] == session
                and recovery["childSessionID"] == child == ident
            )
        if not valid:
            raise Refusal("job-metadata")
        if kind == "subagent" and "outstanding" in recovery:
            outstanding(recovery["outstanding"])
    budget()


def _check_24_state(connection):
    """Refuse new startup work outside the native job/automation tables."""
    # An unregistered server also starts the video poller. It can remove
    # malformed/expired rows, acquire a leader lease, or contact a provider
    # for a durable job created outside a session. Never read its values.
    if (
        connection.execute(
            "SELECT 1 FROM kv WHERE key = 'video-leader' OR "
            "(key >= 'video:' AND key < 'video;') LIMIT 1"
        ).fetchone()
        is not None
    ):
        raise Refusal("video-state")
    _table_schema(
        connection,
        "machine",
        {
            "id": ("TEXT", None, True),
            "time_created": ("INTEGER", True, False),
            "key": ("TEXT", False, False),
        },
        "machine-schema",
    )
    if connection.execute("SELECT 1 FROM machine LIMIT 1").fetchone() is not None:
        raise Refusal("machine-state")


@contextlib.contextmanager
def _native_directory(root, deadline, category):
    """Traverse native storage paths without following links; None is absent."""
    root = Path(root)
    if not root.is_absolute() or ".." in root.parts:
        raise Refusal(category)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(root.anchor, flags)
        try:
            for part in root.parts[1:]:
                if time.monotonic() >= deadline:
                    raise Refusal("deadline")
                try:
                    child = os.open(part, flags, dir_fd=fd)
                except FileNotFoundError:
                    yield None
                    return
                os.close(fd)
                fd = child
            _native_directory_identity(os.fstat(fd), category)
            yield fd
        finally:
            os.close(fd)
    except OSError:
        raise Refusal(category) from None


def _native_directory_identity(info, category):
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
    ):
        raise Refusal(category)
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)


def _check_24_blobs(root, deadline):
    """Do not let native's automatic retention sweep clean up live data."""

    def check(info):
        return _native_directory_identity(info, "blob-state")

    def check_budget():
        if time.monotonic() >= deadline:
            raise Refusal("deadline")

    def recheck(identity):
        with _native_directory(root, deadline, "blob-state") as current_fd:
            current = None if current_fd is None else check(os.fstat(current_fd))
            if current != identity:
                raise Refusal("blob-state")

    try:
        with contextlib.ExitStack() as stack:
            data_fd = stack.enter_context(
                _native_directory(root, deadline, "blob-state")
            )
            if data_fd is None:
                # A fresh host destination need not have a native data root.
                recheck(None)
                return
            data_identity = check(os.fstat(data_fd))
            try:
                blob_fd = os.open(
                    "blobs",
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=data_fd,
                )
            except FileNotFoundError:
                recheck(data_identity)
                check_budget()
                return
            stack.callback(os.close, blob_fd)
            blob_identity = check(os.fstat(blob_fd))
            cutoff = time.time() - BLOB_MAX_AGE
            with os.scandir(blob_fd) as entries:
                for count, entry in enumerate(entries, 1):
                    check_budget()
                    if count > MAX_BLOB_ENTRIES:
                        raise Refusal("blob-state")
                    if entry.name.startswith("blob_"):
                        info = entry.stat(follow_symlinks=False)
                        if (
                            not stat.S_ISREG(info.st_mode)
                            or info.st_uid != os.getuid()
                            or info.st_nlink != 1
                            or info.st_mode & 0o022
                            or info.st_mtime <= cutoff
                        ):
                            raise Refusal("blob-state")
            check_budget()
            if (
                check(os.stat("blobs", dir_fd=data_fd, follow_symlinks=False))
                != blob_identity
            ):
                raise Refusal("blob-state")
            recheck(data_identity)
    except OSError:
        raise Refusal("blob-state") from None


def _check_24_host_service(deadline):
    """Best-effort refusal, not a DB lock. Never read the registration body."""
    root = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local/state"))
    category = "host-service-state"
    root /= "opencode"
    with _native_directory(root, deadline, category) as fd:
        identity = (
            None if fd is None else _native_directory_identity(os.fstat(fd), category)
        )
        if fd is not None:
            with os.scandir(fd) as entries:
                for count, entry in enumerate(entries, 1):
                    if time.monotonic() >= deadline:
                        raise Refusal("deadline")
                    if count > MAX_SERVICE_ENTRIES or re.fullmatch(
                        r"service(?:-[A-Za-z0-9._-]+)?\.json", entry.name
                    ):
                        raise Refusal(category)
    with _native_directory(root, deadline, category) as fd:
        current = (
            None if fd is None else _native_directory_identity(os.fstat(fd), category)
        )
        if current != identity:
            raise Refusal(category)


def _check_database(path, *, allow_missing=False, timeout=1, version=None):
    """Inspect scheduler and explicitly selected job metadata without mutation.

    Disabled schedules/automations flags do NOT disable native startup rewake. All rows,
    including terminal/unknown statuses, therefore refuse service construction.
    Missing schemas are not evidence of an empty scheduler. Never use immutable
    SQLite mode here: it can hide committed schedule rows in an existing WAL.
    The default remains scheduler-only for the independent quiet-source guard.
    """
    profile = snapshot_profile(version)
    if version is not None and profile is None:
        raise Refusal("unsupported-version")
    strict = profile in SUBAGENT_SNAPSHOT_PROFILES
    deadline = time.monotonic() + timeout
    path = Path(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return
        raise Refusal("database-missing") from None
    except OSError:
        raise Refusal("database-unsafe") from None
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or before.st_nlink != 1
        or before.st_mode & 0o022
    ):
        raise Refusal("database-unsafe")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with contextlib.ExitStack() as stack:
            stack.callback(os.close, descriptor)
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise Refusal("database-unsafe")
            if strict and any(
                getattr(opened, key) != getattr(before, key)
                for key in ("st_uid", "st_mode", "st_nlink")
            ):
                raise Refusal("database-unsafe")
            connection = stack.enter_context(
                contextlib.closing(
                    sqlite3.connect(
                        path.absolute().as_uri() + "?mode=ro", uri=True, timeout=timeout
                    )
                )
            )
            if strict:
                connection.set_progress_handler(
                    lambda: int(time.monotonic() >= deadline), 1000
                )
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            scheduler = _check_scheduler(connection, profile)
            if version == "2.3.0" and scheduler == "automation":
                # This known older executable cannot consume the newer layout.
                # Forward migration of an empty schedule store is different from
                # silently admitting an in-place native downgrade.
                raise Refusal("schedule-schema")
            if strict:
                _check_jobs(connection, deadline)
            if profile == SNAPSHOT_24:
                _check_24_state(connection)
            after = path.lstat()
            if strict and any(
                getattr(after, key) != getattr(before, key)
                for key in ("st_mode", "st_nlink")
            ):
                raise Refusal("database-unsafe")
            if (after.st_dev, after.st_ino, after.st_uid) != (
                before.st_dev,
                before.st_ino,
                before.st_uid,
            ) or not stat.S_ISREG(after.st_mode):
                raise Refusal("database-unsafe")
            # Optional callers can retain the ORIGINAL validated binding rather
            # than treating a later lstat/open as a new baseline. Existing
            # Reader callers deliberately ignore this return value.
            return before
    except (OSError, sqlite3.Error, ValueError) as error:
        if isinstance(error, Refusal):
            raise
        if strict and time.monotonic() >= deadline:
            raise Refusal("deadline") from None
        raise Refusal("database-invalid") from None


def fence_environment(env, *, version=None):
    """New public-only environment; retain native store/record path selectors.

    The input is an already constructed private_host_env, not os.environ. In
    particular do not retain arbitrary OPENCODE_ settings or inline credentials.
    """
    names = (
        "HOME",
        "PATH",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "OPENCODE_TEST_HOME",
        "OPENCODE_CONFIG_DIR",
        "OPENCODE_DB",
        "OPENCODE_DISABLE_CHANNEL_DB",
    )
    result = {name: env[name] for name in names if name in env}
    result.update(
        {
            "OPENCODE_FLAGS": "-schedules",
            "OPENCODE_CONFIG_PROJECT_DISABLE": "1",
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "OPENCODE_DISABLE_CLAUDE_CODE": "1",
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
            "OPENCODE_FILEWATCHER_DISABLE": "1",
            "OPENCODE_CONFIG_CONTENT": json.dumps(
                {
                    "$schema": "https://opencode.ai/config.json",
                    "snapshots": False,
                    "formatter": False,
                    "update": "disable",
                    "warming": False,
                    "policies": [
                        {"action": action, "resource": "*", "effect": "deny"}
                        for action in ("plugin.use", "provider.use", "tool.use")
                    ],
                }
            ),
        }
    )
    if snapshot_profile(version) == SNAPSHOT_24:
        result["OPENCODE_FLAGS"] = "-machines,-sqlite_fts"
        config = json.loads(result["OPENCODE_CONFIG_CONTENT"])
        config["automations"] = False
        result["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    return result


def _check_system_config():
    # Native system policy outranks inline policy and can allow integrations.
    # There is no supported CLI switch to suppress it: refuse, never rewrite or
    # weaken it. Recovery is Linux/rootless-Podman only.
    root = Path("/etc/opencode")
    try:
        info = root.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise Refusal("system-config") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
        raise Refusal("system-config")
    try:
        if next(root.iterdir(), None) is not None:
            raise Refusal("system-config")
    except OSError:
        raise Refusal("system-config") from None


def assert_host_safe(box, path, data, env, remaining):
    """Fence a destination before native projection; never query a host service.

    ``debug paths db`` in supported native versions uses Global/databasePath
    only, not service discovery or the database layer. ``path`` is deliberately
    not used to reconstruct private_host_env (which would recurse).
    """
    del path
    try:
        if snapshot_profile(data.get("opencode_version")) is None:
            raise Refusal("unsupported-version")
        _check_system_config()
        release = data["opencode_version"]
        result_env = fence_environment(env, version=release)
        if snapshot_profile(release) == SNAPSHOT_24:
            _check_24_host_service(time.monotonic() + _budget(remaining, 1))
            data_home = result_env.get("XDG_DATA_HOME")
            if not isinstance(data_home, str) or not Path(data_home).is_absolute():
                raise Refusal("blob-state")
            _check_24_blobs(
                Path(data_home) / "opencode", time.monotonic() + _budget(remaining, 1)
            )
        command = box.host_command(data, "debug", "paths", "db")
        version = box.run(
            [command[0], "--version"],
            env=result_env,
            capture=True,
            private=True,
            timeout=_budget(remaining, 10),
        )
        _check_version(version.stdout, data["opencode_version"])
        result = box.run(
            command,
            env=result_env,
            capture=True,
            private=True,
            timeout=_budget(remaining, 15),
        )
        _budget(remaining, 15)
        # Remove the CLI's one line terminator, not filename whitespace.
        candidate = result.stdout.removesuffix("\n")
        if (
            not candidate.startswith("/")
            or "\n" in candidate
            or "\0" in candidate
            or len(candidate) > 4096
        ):
            raise Refusal("host-database-path")
        _check_database(
            candidate,
            allow_missing=True,
            timeout=_budget(remaining, 1),
            version=data["opencode_version"],
        )
        return result_env
    except Refusal as error:
        raise _boxed(box, error.category) from None
    except (OSError, ValueError, subprocess.SubprocessError, box.BoxError):
        raise _boxed(box, "host-database-probe") from None


@contextlib.contextmanager
def _remote_environment(home, *, version=None):
    """Private empty discovery roots, with only the real source data directory."""
    with tempfile.TemporaryDirectory(prefix="oc-box-history-") as temporary:
        root = Path(temporary)
        for name in ("home", "config", "state", "cache"):
            (root / name).mkdir(mode=0o700)
        env = fence_environment(
            {
                "HOME": str(root / "home"),
                "OPENCODE_TEST_HOME": str(root / "home"),
                "PATH": PATH,
                "LANG": "C.UTF-8",
                "TMPDIR": str(root),
                "XDG_CONFIG_HOME": str(root / "config"),
                "OPENCODE_CONFIG_DIR": str(root / "config" / "opencode"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "XDG_DATA_HOME": home + "/.local/share",
                "OPENCODE_DB": "oc-box-v2.db",
            },
            version=version,
        )
        yield root, env


def _remote(argv):
    action, home, *arguments = argv
    password = os.environ.get("OPENCODE_PASSWORD") if action == "command" else None
    # Clear create-time container secrets even from this helper's environment.
    os.environ.clear()
    os.environ.update({"PATH": PATH, "LANG": "C.UTF-8"})
    release = None
    if action == "lease":
        payload = sys.stdin.buffer.readline(MAX_PAYLOAD + 1)
        if len(payload) > MAX_PAYLOAD or not payload.endswith(b"\n"):
            raise Refusal("startup-payload")
        settings = json.loads(payload)
        if (
            not isinstance(settings, dict)
            or snapshot_profile(settings.get("version")) is None
        ):
            raise Refusal("unsupported-version")
        _check_system_config()
        release = settings["version"]
        if snapshot_profile(release) == SNAPSHOT_24:
            _check_24_blobs(Path(home) / ".local/share/opencode", time.monotonic() + 1)
        _check_database(
            Path(home) / ".local/share/opencode/oc-box-v2.db",
            version=settings["version"],
        )
    elif action != "command":
        raise Refusal("startup-payload")
    with _remote_environment(home, version=release) as (root, env):
        os.chdir(root)
        os.environ.clear()
        os.environ.update(env)
        if password is not None:
            os.environ["OPENCODE_PASSWORD"] = password
        if action == "command":
            # Keep the temporary discovery roots alive until the client exits.
            return subprocess.call(arguments, env=os.environ)
        if snapshot_profile(settings.get("version")) is None:
            raise Refusal("unsupported-version")
        version = subprocess.run(
            [arguments[0], "--version"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if version.returncode != 0:
            raise Refusal("unsupported-version")
        _check_version(version.stdout, settings["version"])
        namespace = {"__name__": "oc_box_history_bridge"}
        exec(settings["bridge"], namespace)
        # Reuse only its unregistered serve --stdio server; no managed-service
        # discovery/restart continuity, which can resume native 2.3.x subagents.
        # The public bridge owns/reaps the one native child.
        exit_info = {}
        with namespace["server"](arguments[0], exit_info=exit_info) as request:
            print(json.dumps(request.endpoint), flush=True)
            while request.alive():
                if select.select([sys.stdin], [], [], 0.1)[0] and not os.read(0, 4096):
                    break
        if exit_info != {"code": 0, "forced": False}:
            raise Refusal("cleanup-failed")
    return 0


class _Exports:
    """One demand-driven export helper, scoped inside an existing Reader lease."""

    def __init__(self, box, reader, data):
        self.box, self.reader = box, reader
        self.workspace = data["id"]
        self.version = data["opencode_version"]
        self.directory = box.container_project(data)
        self.endpoint = {key: reader.connection[key] for key in ("url", "password")}
        self.child = self.errors = self.error_file = None
        self.buffer = bytearray()
        self.sequence = 0
        self.failed = False

    def fail(self, category):
        self.failed = True
        return _boxed(self.box, category)

    def deadline(self, maximum=180):
        return time.monotonic() + _budget(self.reader.remaining, maximum)

    def write(self, payload, deadline):
        descriptor = self.child.stdin.fileno()
        offset = 0
        while offset < len(payload):
            timeout = min(
                deadline - time.monotonic(), _budget(self.reader.remaining, 5)
            )
            if timeout <= 0:
                raise self.fail("deadline")
            if select.select([], [descriptor], [], min(0.1, timeout))[1]:
                try:
                    written = os.write(descriptor, payload[offset : offset + 4096])
                    if written <= 0:
                        raise self.fail("history-command")
                    offset += written
                except BlockingIOError:
                    continue

    def read(self, size, deadline, *, line=False):
        while True:
            if line:
                end = self.buffer.find(b"\n")
                if end >= 0:
                    if end >= size:
                        raise self.fail("history-response")
                    value = bytes(self.buffer[:end])
                    del self.buffer[: end + 1]
                    return value
                if len(self.buffer) >= size:
                    raise self.fail("history-response")
            elif len(self.buffer) >= size:
                value = bytes(self.buffer[:size])
                del self.buffer[:size]
                return value
            timeout = min(
                deadline - time.monotonic(), _budget(self.reader.remaining, 180)
            )
            if timeout <= 0:
                raise self.fail("deadline")
            if select.select([self.child.stdout], [], [], min(0.1, timeout))[0]:
                try:
                    chunk = os.read(
                        self.child.stdout.fileno(), min(65536, size - len(self.buffer))
                    )
                except BlockingIOError:
                    continue
                if not chunk:
                    raise self.fail("history-command")
                self.buffer.extend(chunk)

    def start(self):
        bridge = self.box.opencode_v2_io
        command = self.reader.command(
            [
                "python3",
                "-I",
                "-S",
                "-c",
                (self.box.PACKAGE / "opencode_v2_io.py").read_text(),
                "history-worker",
                self.box.CONTAINER_OPENCODE,
            ]
        )
        self.errors = self.box.captured_output(private=True)
        self.error_file = self.errors.__enter__()
        deadline = self.deadline(45)
        with self.box.history_recovery.admit():
            self.child = subprocess.Popen(
                command,
                env=command.environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.error_file,
                start_new_session=True,
            )
        os.set_blocking(self.child.stdin.fileno(), False)
        os.set_blocking(self.child.stdout.fileno(), False)
        payload = (
            json.dumps(
                {
                    "endpoint": self.endpoint,
                    "directory": self.directory,
                    "timeout": _budget(self.reader.remaining, 180),
                }
            )
            + "\n"
        ).encode()
        if len(payload) > bridge.HISTORY_WORKER_CONTROL:
            raise self.fail("history-command")
        # Admission covers each bounded write, not the subsequent history read.
        with self.box.history_recovery.admit():
            self.write(payload, min(deadline, time.monotonic() + 5))
        if json.loads(self.read(4096, deadline, line=True)) != {
            "protocol": bridge.HISTORY_WORKER_PROTOCOL
        }:
            raise self.fail("history-response")

    def run(self, data, action, settings):
        bridge = self.box.opencode_v2_io
        live = self.box.workspace_service.live_connection()
        if (
            self.failed
            or CURRENT.get() is not self.reader
            or data.get("id") != self.workspace
            or data.get("opencode_version") != self.version
            or self.box.container_project(data) != self.directory
            or self.reader.child is None
            or self.reader.child.poll() is not None
            or live is None
            or live.get("workspace") != self.workspace
            or {key: live.get(key) for key in ("url", "password")} != self.endpoint
            or action not in ("sessions", "export")
            or set(settings) != ({"session"} if action == "export" else set())
        ):
            raise self.fail("reader-source")
        try:
            if self.child is None:
                self.start()
            if (
                self.buffer
                or self.child.poll() is not None
                or select.select([self.child.stdout], [], [], 0)[0]
            ):
                raise self.fail("history-response")
            self.sequence += 1
            packet = {"id": self.sequence, "action": action, **settings}
            payload = (json.dumps(packet) + "\n").encode()
            if len(payload) > bridge.HISTORY_WORKER_CONTROL:
                raise self.fail("history-command")
            deadline = self.deadline()
            with self.box.history_recovery.admit():
                self.write(payload, min(deadline, time.monotonic() + 5))
            response = json.loads(self.read(4096, deadline, line=True))
            if (
                not isinstance(response, dict)
                or type(response.get("id")) is not int
                or response["id"] != self.sequence
            ):
                raise self.fail("history-response")
            if set(response) == {"id", "error"}:
                reason = response["error"]
                if not isinstance(reason, str) or reason not in bridge.FIXED_MESSAGES:
                    raise self.fail("history-response")
                self.failed = reason not in bridge.PENDING_MESSAGES
                return subprocess.CompletedProcess([], 2, "", bridge.MARKER + reason)
            if (
                set(response) != {"id", "size"}
                or type(response["size"]) is not int
                or not 0 < response["size"] <= bridge.HISTORY_WORKER_PAYLOAD
            ):
                raise self.fail("history-response")
            value = self.read(response["size"], deadline).decode("utf-8")
            _budget(self.reader.remaining, 180)
            return subprocess.CompletedProcess([], 0, value, "")
        except Refusal as error:
            raise self.fail(error.category) from None
        except (
            self.box.history_recovery.RecoveryError,
            self.box.workspace_control.ControlError,
        ):
            self.failed = True
            raise
        except (OSError, ValueError, subprocess.SubprocessError):
            raise self.fail("history-response") from None

    def close(self):
        failed, interruption = False, None

        def caught(error):
            nonlocal failed, interruption
            failed = True
            if interruption is None and isinstance(
                error, (KeyboardInterrupt, SystemExit)
            ):
                interruption = error

        if self.child is not None:
            try:
                failed = (
                    bool(self.buffer)
                    or self.child.poll() is not None
                    or bool(select.select([self.child.stdout], [], [], 0)[0])
                )
            except BaseException as error:
                caught(error)
            try:
                self.child.stdin.close()
            except OSError:
                pass
            except BaseException as error:
                caught(error)
            try:
                failed = self.child.wait(timeout=10) != 0 or failed
            except BaseException as error:
                caught(error)
                try:
                    os.killpg(self.child.pid, signal.SIGKILL)
                except OSError:
                    pass
                except BaseException as error:
                    caught(error)
                try:
                    self.child.wait(timeout=5)
                except BaseException as error:
                    caught(error)
            try:
                # Waiting can itself make delayed trailing output readable.
                # A reaped helper must leave exact EOF, not another reply.
                if self.buffer or os.read(self.child.stdout.fileno(), 1):
                    failed = True
            except BaseException as error:
                caught(error)
            try:
                self.child.stdout.close()
            except BaseException as error:
                caught(error)
            self.child = None
        if self.error_file is not None:
            try:
                self.errors.__exit__(None, None, None)
            except BaseException as error:
                caught(error)
            self.error_file = None
        if interruption is not None:
            if isinstance(interruption, SystemExit) and interruption.code in (None, 0):
                interruption.code = 1
            raise interruption
        if failed:
            raise self.fail("history-cleanup")


@contextlib.contextmanager
def export_pass(box, data):
    """Amortize transport only for the already fenced post-stop Reader."""
    reader = CURRENT.get()
    if reader is None:
        yield
        return
    if EXPORTS.get() is not None or data.get("id") != reader.data["id"]:
        raise _boxed(box, "reader-reuse")
    if reader.connection is None:
        raise _boxed(box, "reader-source")
    stream = _Exports(box, reader, data)
    token = EXPORTS.set(stream)
    original = None
    try:
        yield
    except BaseException as error:
        original = error
        raise
    finally:
        EXPORTS.reset(token)
        try:
            stream.close()
        except BaseException as cleanup:
            if original is None:
                cleanup.history_source_cleanup_failed = True
                raise
            if isinstance(original, SystemExit) and original.code in (None, 0):
                original.code = 1
            if isinstance(cleanup, (KeyboardInterrupt, SystemExit)):
                cleanup.history_source_cleanup_failed = True
                if isinstance(cleanup, SystemExit) and cleanup.code in (None, 0):
                    cleanup.code = 1
                raise
            # Keep the original error/interruption authoritative, but never
            # silently turn uncertain helper cleanup into an apparent success.
            original.history_source_cleanup_failed = True
            if __package__:
                from . import lifecycle_output, startup_output
            else:
                import lifecycle_output
                import startup_output
            message = (
                "History export helper cleanup unverified. "
                "Container data retained; retry with 'oc-box sync'."
            )
            if lifecycle_output.concise():
                lifecycle_output.fixed_detail("warn", message)
            else:
                with contextlib.suppress(OSError, ValueError):
                    startup_output.line(f"oc-box: {message}", file=sys.stderr)
        else:
            if original is None and stream.failed:
                raise stream.fail("history-command")


class Reader:
    """One unregistered private native lease; never retry the same instance."""

    def __init__(self, box, data, *, remaining, runtime_digest=None):
        self.box, self.data, self.remaining = box, dict(data), remaining
        self._runtime_digest = runtime_digest
        self.child = self.errors = self.error_file = None
        self.connection = None
        self.tokens = []
        self.started = False

    def command(self, arguments):
        if self.connection is None:
            raise _boxed(self.box, "reader-ended")
        args = list(arguments)
        if self.connection is not None:
            if args and args[0] == self.box.CONTAINER_OPENCODE:
                if any(
                    value in args for value in ("--standalone", "serve", "service")
                ) or any(value.startswith("--server=") for value in args):
                    raise _boxed(self.box, "client-command")
                if "--server" not in args:
                    args.extend(["--server", self.connection["url"]])
                elif args.count("--server") != 1 or args[
                    args.index("--server") + 1 : args.index("--server") + 2
                ] != [self.connection["url"]]:
                    raise _boxed(self.box, "client-command")
                if args not in (
                    [
                        self.box.CONTAINER_OPENCODE,
                        "api",
                        "get",
                        "/api/session/active",
                        "--server",
                        self.connection["url"],
                    ],
                    [
                        self.box.CONTAINER_OPENCODE,
                        "api",
                        "get",
                        "/api/experimental/job",
                        "--server",
                        self.connection["url"],
                    ],
                    [
                        self.box.CONTAINER_OPENCODE,
                        "session",
                        "list",
                        "--server",
                        self.connection["url"],
                    ],
                ):
                    raise _boxed(self.box, "client-command")
            elif not (
                (
                    len(args) == 5
                    and args[:2] == ["python3", "-c"]
                    and args[2] == (self.box.PACKAGE / "opencode_v2_io.py").read_text()
                    and args[3] in ("sessions", "export")
                    and args[4] == self.box.CONTAINER_OPENCODE
                )
                or (
                    len(args) == 7
                    and args[:4] == ["python3", "-I", "-S", "-c"]
                    and args[4] == (self.box.PACKAGE / "opencode_v2_io.py").read_text()
                    and args[5] == "history-worker"
                    and args[6] == self.box.CONTAINER_OPENCODE
                )
            ):
                raise _boxed(self.box, "client-command")
        return self._helper_command("command", args)

    def _container_command(self, arguments):
        env = self.box.maintenance_host_env()
        command = ["podman", "exec", "-i", "--workdir", "/"]
        if self.connection is not None:
            env = {**env, "OPENCODE_PASSWORD": self.connection["password"]}
            command.extend(["--env", "OPENCODE_PASSWORD"])
        # env -i removes create-time secrets; only our ephemeral control token
        # is reintroduced by name, never in argv or saved config.
        clean = ["/usr/bin/env", "-i", "PATH=" + PATH, "LANG=C.UTF-8"]
        if self.connection is not None:
            # Read the exec-injected token before resetting the environment,
            # with Python's environment/site hooks disabled, then pass it in memory.
            bootstrap = "import os,sys; p=os.environ['OPENCODE_PASSWORD']; e={'PATH':sys.argv[1],'LANG':'C.UTF-8','OPENCODE_PASSWORD':p}; os.execvpe(sys.argv[2],sys.argv[2:],e)"
            prefix = ["/usr/bin/python3", "-I", "-S", "-c", bootstrap, PATH]
        else:
            prefix = clean
        command.extend([self.data["container"], *prefix, *arguments])
        return self.box.ContainerCommand(command, env)

    def _helper_command(self, action, arguments):
        command = [
            "python3",
            "-I",
            "-S",
            "-c",
            (self.box.PACKAGE / "history_reader.py").read_text(),
            action,
            self.data["home"],
            *arguments,
        ]
        if self._runtime_digest is not None:
            # Host-only import. The unchanged, isolated guard replaces the
            # native path with its verified descriptor before the Reader can
            # execute a version probe, server, or subsequent export helper.
            if __package__:
                from . import runtime_safeguard
            else:
                import runtime_safeguard
            if (
                type(self._runtime_digest) is not str
                or re.fullmatch(r"[a-f0-9]{64}", self._runtime_digest) is None
                or self.box.CONTAINER_OPENCODE
                != runtime_safeguard.PREFIX + "/bin/opencode"
                or self.box.CONTAINER_OPENCODE not in arguments
            ):
                raise Refusal("runtime-executable-unverified")
            command = runtime_safeguard.bound_command(
                command, self._runtime_digest, no_site=True
            )
        return self._container_command(command)

    def _startup_category(self):
        self.error_file.seek(0)
        lines = (
            self.error_file.read(MAX_CONTROL)
            .decode("utf-8", errors="replace")
            .splitlines()
        )
        return next(
            (
                line[len(MARKER) :]
                for line in reversed(lines)
                if line.startswith(MARKER) and line[len(MARKER) :] in GUARD_CATEGORIES
            ),
            "runtime-executable-unverified"
            if self._runtime_digest is not None and self.child.returncode == 126
            else "startup-failed",
        )

    def _write_payload(self, payload, deadline):
        descriptor = self.child.stdin.fileno()
        os.set_blocking(descriptor, False)
        offset = 0
        while offset < len(payload):
            budget = min(_budget(self.remaining, 45), deadline - time.monotonic())
            if budget <= 0:
                raise Refusal("startup-timeout")
            if not select.select([], [descriptor], [], min(0.1, budget))[1]:
                continue
            try:
                written = os.write(descriptor, payload[offset : offset + 4096])
            except BlockingIOError:
                continue
            if written <= 0:
                raise Refusal("startup-failed")
            offset += written

    def __enter__(self):
        # Host-only imports must stay here: the remote __main__ is stdlib-only.
        if __package__:
            from . import workspace_service, history_recovery
        else:
            import workspace_service
            import history_recovery

        if self.started or CURRENT.get() is not None:
            raise _boxed(self.box, "reader-reuse")
        try:
            self.started = True
            if snapshot_profile(self.data.get("opencode_version")) is None:
                raise Refusal("unsupported-version")
            if (
                not isinstance(self.data.get("container"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", self.data["container"])
                or not isinstance(self.data.get("home"), str)
                or not self.data["home"].startswith("/")
            ):
                raise Refusal("reader-source")
            deadline = time.monotonic() + _budget(self.remaining, 45)
            command = self._helper_command("lease", [self.box.CONTAINER_OPENCODE])
            self.errors = self.box.captured_output(private=True)
            self.error_file = self.errors.__enter__()
            with history_recovery.admit():
                self.child = subprocess.Popen(
                    command,
                    env=command.environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=self.error_file,
                    start_new_session=True,
                )
            payload = (
                json.dumps(
                    {
                        "bridge": (self.box.PACKAGE / "opencode_v2_io.py").read_text(),
                        "version": self.data["opencode_version"],
                    }
                ).encode()
                + b"\n"
            )
            if len(payload) > MAX_PAYLOAD:
                raise Refusal("startup-payload")
            self._write_payload(payload, deadline)
            output = bytearray()
            while b"\n" not in output:
                budget = min(_budget(self.remaining, 45), deadline - time.monotonic())
                if budget <= 0:
                    raise Refusal("startup-timeout")
                if self.child.poll() is not None:
                    raise Refusal(self._startup_category())
                if select.select([self.child.stdout], [], [], min(0.1, budget))[0]:
                    chunk = os.read(
                        self.child.stdout.fileno(), MAX_CONTROL + 1 - len(output)
                    )
                    if not chunk:
                        # EOF can precede waitpid's visible exit status. Reap
                        # boundedly before reading the fixed guard marker, never
                        # replace a source schedule refusal with a race-dependent
                        # generic startup error.
                        self.child.wait(timeout=min(1, budget))
                        raise Refusal(self._startup_category())
                    output.extend(chunk)
                if len(output) > MAX_CONTROL:
                    raise Refusal("startup-response")
            line, trailing = output.split(b"\n", 1)
            if trailing.strip():
                raise Refusal("startup-response")
            endpoint = workspace_service.validate_endpoint(json.loads(line))
            self.connection = {
                "url": endpoint["url"],
                "password": endpoint["password"],
                "workspace": self.data["id"],
            }
            self.tokens.append(
                (
                    workspace_service.CONNECTION,
                    workspace_service.CONNECTION.set(self.connection),
                )
            )
            self.tokens.append((CURRENT, CURRENT.set(self)))
            self.assert_idle()
            return self
        except BaseException as error:
            with contextlib.suppress(self.box.BoxError):
                self.shutdown()
            if isinstance(
                error,
                (
                    KeyboardInterrupt,
                    SystemExit,
                    history_recovery.RecoveryError,
                    history_recovery.workspace_control.ControlError,
                ),
            ):
                raise
            category = (
                error.category if isinstance(error, Refusal) else "startup-failed"
            )
            raise _boxed(self.box, category) from None

    def assert_idle(self):
        """Explicit authenticated foreground query; never inspect historical jobs."""
        try:
            if self.connection is None or self.child.poll() is not None:
                raise Refusal("reader-ended")
            # The lease has exactly one control record and no streaming output.
            if select.select([self.child.stdout], [], [], 0)[0]:
                raise Refusal("startup-response")
            result = self.box.run(
                self.command(
                    [
                        self.box.CONTAINER_OPENCODE,
                        "api",
                        "get",
                        "/api/session/active",
                        "--server",
                        self.connection["url"],
                    ],
                ),
                capture=True,
                private=True,
                timeout=_budget(self.remaining, 15),
            )
            _budget(self.remaining, 15)
            response = json.loads(result.stdout)
            if not isinstance(response, dict) or not isinstance(
                response.get("data"), dict
            ):
                raise Refusal("active-response")
            if response["data"]:
                raise Refusal("foreground-active")
        except Refusal as error:
            raise _boxed(self.box, error.category) from None
        except (OSError, ValueError, subprocess.SubprocessError, self.box.BoxError):
            raise _boxed(self.box, "active-query") from None

    def shutdown(self):
        # Cleanup has its own finite allowance even after the caller's deadline.
        # Killing the wrapper is not proof of container stop; the caller owns it.
        try:
            if self.child is not None:
                try:
                    if self.child.stdin:
                        # A remote refusal can close its input before we do.
                        # A broken control pipe must not skip reaping.
                        with contextlib.suppress(OSError):
                            self.child.stdin.close()
                    try:
                        code = self.child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(self.child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        self.child.wait(timeout=5)
                        raise _boxed(self.box, "cleanup-failed") from None
                    if code != 0:
                        raise _boxed(self.box, "cleanup-failed")
                finally:
                    if self.child.stdout:
                        self.child.stdout.close()
                    self.child = None
        except (OSError, subprocess.SubprocessError):
            raise _boxed(self.box, "cleanup-failed") from None
        finally:
            for variable, token in reversed(self.tokens):
                variable.reset(token)
            self.tokens.clear()
            self.connection = None
            if self.error_file is not None:
                self.errors.__exit__(None, None, None)
                self.error_file = None

    def __exit__(self, kind, value, traceback):
        if kind is not None:
            with contextlib.suppress(self.box.BoxError):
                self.shutdown()
            return
        try:
            self.assert_idle()
        except BaseException:
            with contextlib.suppress(self.box.BoxError):
                self.shutdown()
            raise
        self.shutdown()


if __name__ == "__main__":
    try:
        sys.exit(_remote(sys.argv[1:]))
    except Refusal as error:
        category = (
            error.category if error.category in GUARD_CATEGORIES else "startup-failed"
        )
        print(MARKER + category, file=sys.stderr)
        sys.exit(2)
    except Exception:
        print(MARKER + "startup-failed", file=sys.stderr)
        sys.exit(1)
