"""One-shot private WAL metadata observer for stock OpenCode 2.4.2.

Run this source with ``python3 -I -S -B -c`` in the already verified live
container, with three private stdio pipes, the project cwd and a request on
stdin. The caller supplies the authenticated /api/info PID, not credentials.
One stable read snapshot is evidence about persisted rows only; it does not
prove the absence of live work or grant authority to cancel anything.
"""

from contextlib import ExitStack, closing
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import resource
import select
import signal
import stat
import sys
import time


VERSION = "2.4.2"
MAX_TARGETS = 16
_MAX_INPUT = 65536
_MAX_PATH = 4095
_MAX_COMPONENTS = 32
_DEADLINE = 8
_MAX_DISCOVERY = 64
_MAX_COUNTS = 256
_MAX_FDINFO = 4096
_MAX_NATIVE_FDS = 4096
_MAX_SELF_FDS = 128
_MAX_SQL_VALUE = 32 * 1024 * 1024
_MAX_ADDRESS_SPACE = 256 * 1024 * 1024
_MAX_NATIVE_TIME = 2**53 - 1
_NATIVE = "/opt/oc-box/runtime/bin/opencode"
_DB = "oc-box-v2.db"
_FILES = (_DB, _DB + "-wal", _DB + "-shm")
_SESSION_ID = re.compile(r"ses_[0-9a-f]{12}[0-9A-Za-z]{14}")
_MESSAGE_ID = re.compile(r"msg_[0-9a-f]{12}[0-9A-Za-z]{14}")
# Native stores the agent ID on the session, but the display name on jobs
# made by the subagent tool. Accept only the known built-in pairs.
_AGENT_LABELS = {"general": "General", "explore": "Explore"}
_JOB_STATUSES = ("running", "completed", "error", "cancelled")
_KINDS = (
    "assistant",
    "user",
    "shell",
    "system",
    "synthetic",
    "idle",
    "compaction",
    "agent-switched",
    "model-switched",
    "location-switched",
    "skill",
)


class _Refusal(Exception):
    """The argument is a closed diagnostic category, never native data."""


def _require(condition, category):
    if not condition:
        raise _Refusal(category)


def _unique_pairs(pairs):
    record = dict(pairs)
    _require(len(record) == len(pairs), "request")
    return record


def _path(value):
    _require(type(value) is str and value.startswith("/"), "request")
    raw = value.encode("utf-8")
    parts = raw[1:].split(b"/")
    _require(
        len(raw) <= _MAX_PATH
        and len(parts) <= _MAX_COMPONENTS
        and all(part not in (b"", b".", b"..") and len(part) <= 255 for part in parts)
        and all(byte >= 32 and byte != 127 for byte in raw),
        "request",
    )
    return Path(value)


def _id(pattern, value):
    return type(value) is str and pattern.fullmatch(value) is not None


def _request():
    raw = sys.stdin.buffer.read(_MAX_INPUT + 1)
    _require(len(raw) <= _MAX_INPUT, "request")
    try:
        request = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
        _require(
            type(request) is dict
            and request.keys() == {"version", "directory", "home", "pid", "targets"}
            and type(request["pid"]) is int
            and 0 < request["pid"] < 2**31
            and request["pid"] != os.getpid(),
            "request",
        )
        _require(request["version"] == VERSION, "version")
        directory = _path(request["directory"])
        db = _path(str(_path(request["home"]) / ".local/share/opencode" / _DB))
        targets = request["targets"]
        if targets is not None:
            _require(
                type(targets) is list and 0 < len(targets) <= MAX_TARGETS, "request"
            )
            for target in targets:
                _require(
                    type(target) is dict
                    and target.keys() == {"session", "message"}
                    and _id(_SESSION_ID, target["session"])
                    and _id(_MESSAGE_ID, target["message"]),
                    "request",
                )
            _require(
                len({target["session"] for target in targets}) == len(targets),
                "request",
            )
        return directory, db, request["pid"], targets
    except (ValueError, RecursionError):
        raise _Refusal("request") from None


def _open(stack, path, flags, *, dir_fd=None):
    fd = os.open(path, flags | os.O_CLOEXEC, dir_fd=dir_fd)
    stack.callback(os.close, fd)
    return fd


def _directory(stack, path):
    flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = _open(stack, "/", flags)
    for part in path.parts[1:]:
        fd = _open(stack, part, flags, dir_fd=fd)
    return fd


def _fd_names(directory, maximum, category):
    names = []
    with os.scandir(directory) as entries:
        for index, entry in enumerate(entries):
            _require(
                index < maximum and entry.name.isascii() and entry.name.isdecimal(),
                category,
            )
            names.append(entry.name)
    return names


def _proc_bytes(process, relative, limit, category):
    fd = os.open(
        relative,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        dir_fd=process,
    )
    with os.fdopen(fd, "rb") as stream:
        raw = stream.read(limit + 1)
    _require(len(raw) <= limit, category)
    return raw


def _proc_field(process, fd, field, category):
    lines = _proc_bytes(process, f"fdinfo/{fd}", _MAX_FDINFO, category).splitlines()
    values = [line[len(field) :].strip() for line in lines if line.startswith(field)]
    _require(len(values) == 1 and re.fullmatch(rb"[0-9]{1,20}", values[0]), category)
    return int(values[0])


def _backing(info):
    return info.st_dev, info.st_ino


def _stamp(info):
    return (*_backing(info), info.st_uid, info.st_gid, info.st_mode, info.st_nlink)


def _identity(process, fd):
    return (
        *_stamp(os.fstat(fd)),
        _proc_field(process, fd, b"mnt_id:", "descriptor-state"),
    )


def _self_fds(observer):
    with ExitStack() as stack:
        directory = _open(
            stack, "fd", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=observer
        )
        names = _fd_names(directory, _MAX_SELF_FDS, "descriptor-state")
        # Neither the directory nor scandir's own handle belongs to the caller.
    for name in names:
        fd = int(name)
        try:
            yield fd, os.fstat(fd)
        except OSError as error:
            if error.errno != errno.EBADF:  # /proc iteration's own descriptor
                raise


class _Filter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("value", ctypes.c_uint32),
    ]


class _Program(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("filter", ctypes.POINTER(_Filter))]


def _deny_inode_metadata_changes(libc, machine):
    # SQLite's Unix VFS can fchown a readonly SHM file when euid is 0,
    # and robust_open can fchmod an empty file. Landlock does not cover these.
    # Match the Linux seccomp architecture before matching syscall numbers.
    arch, changes = {
        # chmod, fchmod, chown, fchown, lchown, utime, utimes, fchownat,
        # futimesat, fchmodat, utimensat, fchmodat2.
        "x86_64": (0xC000003E, (90, 91, 92, 93, 94, 132, 235, 260, 261, 268, 280, 452)),
        # fchmod, fchmodat, fchownat, fchown, utimensat, fchmodat2.
        "aarch64": (0xC00000B7, (52, 53, 54, 55, 88, 452)),
    }[machine]
    load_word, jump_equal, return_value = 0x20, 0x15, 0x06
    deny = (return_value, 0, 0, 0x00050000 | errno.EPERM)
    rules = [(load_word, 0, 0, 4), (jump_equal, 1, 0, arch), deny, (load_word, 0, 0, 0)]
    for number in changes:
        rules.extend(((jump_equal, 0, 1, number), deny))
    rules.append((return_value, 0, 0, 0x7FFF0000))
    instructions = (_Filter * len(rules))(*(_Filter(*rule) for rule in rules))
    program = _Program(len(rules), instructions)
    _require(libc.prctl(22, 2, ctypes.byref(program), 0, 0) == 0, "write-isolation")


def _seal(libc, observer, source):
    # One disposable task, with no inherited filesystem write descriptors.
    # Deny write/creation/removal/refer/truncate globally before SQLite starts.
    with ExitStack() as stack:
        tasks = _open(
            stack, "task", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=observer
        )
        _require(
            _fd_names(tasks, 1, "write-isolation") == [str(os.getpid())],
            "write-isolation",
        )
    inherited = dict(_self_fds(observer))
    _require(
        inherited.keys() == source.sqlite_baseline
        and all(
            stat.S_ISFIFO(inherited[fd].st_mode) or stat.S_ISSOCK(inherited[fd].st_mode)
            for fd in (0, 1, 2)
        )
        and all(
            flags & os.O_PATH
            or stat.S_ISDIR(inherited[fd].st_mode)
            and flags & os.O_ACCMODE == os.O_RDONLY
            for fd in inherited.keys() - {0, 1, 2, source.pidfd}
            for flags in (fcntl.fcntl(fd, fcntl.F_GETFL),)
        ),
        "write-isolation",
    )
    machine = os.uname().machine
    _require(
        machine in ("x86_64", "aarch64") and ctypes.sizeof(ctypes.c_void_p) == 8,
        "write-isolation",
    )
    libc.syscall.restype = ctypes.c_long
    create_ruleset, restrict_self = 444, 446
    _require(libc.syscall(create_ruleset, None, 0, 1) >= 3, "write-isolation")
    rights = ctypes.c_uint64(sum(1 << bit for bit in (1, *range(4, 15))))
    ruleset = libc.syscall(
        create_ruleset, ctypes.byref(rights), ctypes.sizeof(rights), 0
    )
    _require(ruleset >= 0, "write-isolation")
    try:
        _require(libc.prctl(38, 1, 0, 0, 0) == 0, "write-isolation")  # NO_NEW_PRIVS
        _require(libc.syscall(restrict_self, ruleset, 0) == 0, "write-isolation")
    finally:
        os.close(ruleset)
    _deny_inode_metadata_changes(libc, machine)


class _Source:
    def __init__(self, stack, proc, observer, directory, db, pid):
        self.observer = observer
        self.pid = pid
        self.pidfd = os.pidfd_open(pid)
        stack.callback(os.close, self.pidfd)
        self.process = _open(
            stack, str(pid), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=proc
        )
        self.poll = select.poll()
        self.poll.register(self.pidfd, select.POLLIN)
        self.process_stamp = self._process_stamp()
        self.directory, self.db = directory, db
        self.project_stamp = _stamp(os.fstat(_directory(stack, directory)))
        self.parent = _directory(stack, db.parent)
        self.parent_identity = _identity(self.observer, self.parent)
        self.entries = tuple(
            _open(stack, name, os.O_PATH | os.O_NOFOLLOW, dir_fd=self.parent)
            for name in _FILES
        )
        self.files = tuple(_identity(self.observer, fd) for fd in self.entries)
        parent_info = os.fstat(self.parent)
        _require(
            parent_info.st_uid == os.geteuid()
            and not parent_info.st_mode & 0o022
            and len({item[:2] for item in self.files}) == len(_FILES),
            "path-binding",
        )
        for fd, file_identity in zip(self.entries, self.files):
            info = os.fstat(fd)
            _require(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == parent_info.st_uid
                and info.st_nlink == 1
                and not info.st_mode & 0o022
                and info.st_dev == parent_info.st_dev
                and file_identity[-1] == self.parent_identity[-1],
                "path-binding",
            )
        self.sqlite_baseline = {fd for fd, _ in _self_fds(self.observer)}

    def _process_stamp(self):
        _require(
            not self.poll.poll(0)
            and _proc_field(self.observer, self.pidfd, b"Pid:", "process-binding")
            == self.pid
            and os.fstat(self.process).st_uid == os.geteuid()
            and _backing(os.stat("ns/mnt", dir_fd=self.process))
            == _backing(os.stat("ns/mnt", dir_fd=self.observer))
            and _backing(os.stat("root", dir_fd=self.process))
            == _backing(os.stat("root", dir_fd=self.observer)),
            "process-binding",
        )
        raw = _proc_bytes(self.process, "stat", 8192, "process-binding")
        prefix, end, tail = raw.rpartition(b") ")
        fields = tail.split()
        executable = os.stat("exe", dir_fd=self.process)
        expected = os.stat(_NATIVE)
        identity = _stamp(executable)
        _require(
            end
            and prefix.startswith(f"{self.pid} (".encode("ascii"))
            and len(fields) > 19
            and fields[0] not in (b"Z", b"X", b"x")
            and re.fullmatch(rb"[1-9][0-9]{0,19}", fields[19])
            and stat.S_ISREG(executable.st_mode)
            and identity == _stamp(expected)
            and not self.poll.poll(0),
            "process-binding",
        )
        return (
            fields[19],
            identity,
            executable.st_size,
            executable.st_ctime_ns,
            executable.st_mtime_ns,
        )

    def _native_fds(self):
        found = set()
        expected = {item[:2]: item for item in self.files}
        with ExitStack() as stack:
            fds = _open(
                stack,
                "fd",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=self.process,
            )
            for name in _fd_names(fds, _MAX_NATIVE_FDS, "process-descriptors"):
                try:
                    info = os.stat(name, dir_fd=fds)
                    identity = expected.get(_backing(info))
                    if identity is None or identity in found:
                        continue
                    mount = _proc_field(
                        self.process, name, b"mnt_id:", "process-descriptors"
                    )
                    _require(
                        (*_stamp(info), mount) == identity
                        and _stamp(os.stat(name, dir_fd=fds)) == _stamp(info),
                        "process-descriptors",
                    )
                    found.add(identity)
                except FileNotFoundError:
                    # Individual native descriptors can close during enumeration.
                    continue
        _require(found == set(self.files), "process-descriptors")

    def recheck(self, *, native=False):
        with ExitStack() as stack:
            project = _directory(stack, self.directory)
            parent = _directory(stack, self.db.parent)
            _require(
                os.getcwd() == str(self.directory)
                and _stamp(os.fstat(project))
                == _stamp(os.stat("."))
                == self.project_stamp
                and _identity(self.observer, parent)
                == _identity(self.observer, self.parent)
                == self.parent_identity,
                "path-binding",
            )
            for name, fd, expected in zip(_FILES, self.entries, self.files):
                current = _open(stack, name, os.O_PATH | os.O_NOFOLLOW, dir_fd=parent)
                _require(
                    _identity(self.observer, current)
                    == _identity(self.observer, fd)
                    == expected,
                    "path-binding",
                )
            try:
                os.stat(_DB + "-journal", dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise _Refusal("path-binding")
        _require(not self.poll.poll(0), "process-binding")
        if native:
            _require(self._process_stamp() == self.process_stamp, "process-binding")
            self._native_fds()
            _require(self._process_stamp() == self.process_stamp, "process-binding")

    def sqlite_fds(self, expected, *, opening=False):
        opened = {}
        for fd, _ in _self_fds(self.observer):
            if fd in self.sqlite_baseline:
                continue
            identity = _identity(self.observer, fd)
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            _require(
                identity not in opened
                and identity in expected
                and flags & (os.O_PATH | os.O_ACCMODE) == os.O_RDONLY,
                "sqlite-descriptors",
            )
            opened[identity] = fd
        _require(
            self.files[0] in opened if opening else opened.keys() == set(expected),
            "sqlite-descriptors",
        )
        return opened


# The 2.4.2 migrations and SQL definitions describe ordinary main tables. Check
# their columns, affinities and primary keys before projecting any native rows.
_SCHEMA = {
    "session_v2": {
        "TEXT": (
            "id project_id workspace_id parent_id fork_session_id fork_boundary slug "
            "directory path title version share_url summary_diffs metadata revert "
            "permission agent model idle_outcome"
        ),
        "INTEGER": (
            "summary_additions summary_deletions summary_files tokens_input tokens_output "
            "tokens_reasoning tokens_cache_read tokens_cache_write time_created time_updated "
            "time_compacting time_archived time_suspended resume_attempts time_idle time_viewed time_busy"
        ),
        "REAL": "cost",
    },
    "session_message": {
        "TEXT": "id session_id type data",
        "INTEGER": "seq time_created time_updated",
    },
    "session_inbox": {
        "TEXT": "id session_id type payload delivery",
        "INTEGER": "enqueued_seq time_created",
    },
    "job": {
        "TEXT": "notification_id id session_id child_session_id kind title status output error recovery",
        "INTEGER": "time_created time_updated",
    },
}
_COLUMNS = {
    "session_v2": (
        "id project_id workspace_id parent_id directory agent revert time_suspended "
        "time_archived time_compacting time_busy time_idle"
    ).split(),
    "session_message": ("id", "session_id", "type", "seq", "data"),
    "session_inbox": ("session_id",),
    "job": (
        "notification_id",
        "id",
        "session_id",
        "child_session_id",
        "kind",
        "status",
        "recovery",
    ),
}


def _probe_schema(connection):
    for table, by_type in _SCHEMA.items():
        expected = {
            name: kind for kind, names in by_type.items() for name in names.split()
        }
        key = "notification_id" if table == "job" else "id"
        catalog = connection.execute(f"PRAGMA main.table_list('{table}')").fetchmany(2)
        _require(
            catalog == [("main", table, "table", len(expected), 0, 0)], "sqlite-schema"
        )
        columns = connection.execute(f"PRAGMA main.table_xinfo('{table}')").fetchmany(
            len(expected) + 1
        )
        _require(
            len(columns) == len(expected)
            and {row[1]: row[2].upper() for row in columns} == expected
            and all(row[5] == int(row[1] == key) and row[6] == 0 for row in columns),
            "sqlite-schema",
        )


def _authorizer(sqlite):
    def authorize(action, target, arg, database, trigger):
        allowed = trigger is None and (
            action == sqlite.SQLITE_SELECT
            or (action == sqlite.SQLITE_TRANSACTION and target == "ROLLBACK")
            or (
                action == sqlite.SQLITE_PRAGMA
                and target == "data_version"
                and arg is None
                and database == "main"
            )
            or (
                action == sqlite.SQLITE_READ
                and database == "main"
                and target in _COLUMNS
                and (arg == "" or arg in _COLUMNS[target])
            )
            or (
                action == sqlite.SQLITE_FUNCTION
                and arg
                in (
                    "count",
                    "length",
                    "max",
                    "typeof",
                    "json_type",
                    "json_extract",
                    "json_array_length",
                )
            )
        )
        return sqlite.SQLITE_OK if allowed else sqlite.SQLITE_DENY

    return authorize


_RELATION = """
FROM main.session_v2 AS s
JOIN main.session_v2 AS p ON p.id = s.parent_id AND p.directory = s.directory
  AND p.project_id = s.project_id AND p.workspace_id IS s.workspace_id
"""
_DISCOVERY_SQL = f"""
SELECT DISTINCT CASE WHEN length(s.id) = 30 THEN s.id END
{_RELATION}
JOIN main.job AS j ON j.child_session_id = s.id AND j.session_id = p.id AND j.id = s.id
JOIN main.session_message AS m ON m.session_id = s.id AND m.seq = (
  SELECT max(tail.seq) FROM main.session_message AS tail WHERE tail.session_id = s.id
)
WHERE s.directory = ? AND s.id != p.id AND p.parent_id IS NULL
  AND s.time_archived IS NULL AND p.time_archived IS NULL
  AND s.revert IS NULL AND p.revert IS NULL AND s.agent IN ('general', 'explore')
  AND j.kind = 'subagent' AND j.status = 'running'
  AND m.type = 'assistant' AND json_type(m.data, '$.time.completed') IS NULL
ORDER BY 1 LIMIT ?
"""


def _defined_times(columns):
    return " AND ".join(
        f"({column} IS NULL OR (typeof({column}) = 'integer' "
        f"AND {column} BETWEEN 0 AND {_MAX_NATIVE_TIME}))"
        for column in columns
    )


_SESSION_SQL = f"""
SELECT CASE WHEN length(p.id) = 30 THEN p.id END,
       CASE WHEN s.agent IN ('general', 'explore') THEN s.agent END,
       s.time_suspended IS NOT NULL,
       s.time_busy IS NOT NULL AND (s.time_idle IS NULL OR s.time_busy > s.time_idle),
       s.time_compacting IS NOT NULL, s.time_archived IS NOT NULL,
       s.revert IS NOT NULL, p.revert IS NOT NULL,
       p.parent_id IS NOT NULL, p.time_archived IS NOT NULL,
       {
    _defined_times(
        (
            "s.time_suspended",
            "s.time_busy",
            "s.time_idle",
            "s.time_compacting",
            "s.time_archived",
            "p.time_archived",
        )
    )
},
       (SELECT count(*) FROM (
          SELECT 1 FROM main.session_v2 AS child WHERE child.parent_id = s.id LIMIT {
    _MAX_COUNTS + 1
}
       )),
       (SELECT count(*) FROM (
          SELECT 1 FROM main.session_inbox AS entry WHERE entry.session_id = s.id LIMIT {
    _MAX_COUNTS + 1
}
       ))
{_RELATION}
WHERE s.directory = ? AND s.id = ? AND s.id != p.id
LIMIT 2
"""
_COMPLETION = f"""
CASE
  WHEN m.type != 'assistant' THEN 0
  WHEN json_type(m.data, '$.time.created') IS NOT 'integer' THEN NULL
  WHEN json_extract(m.data, '$.time.created') NOT BETWEEN 0 AND {_MAX_NATIVE_TIME} THEN NULL
  WHEN json_type(m.data, '$.time.completed') IS NULL THEN 0
  WHEN json_type(m.data, '$.time.completed') = 'integer'
    AND json_extract(m.data, '$.time.completed') BETWEEN 0 AND {_MAX_NATIVE_TIME} THEN 1
END
"""
_TAIL_SQL = f"""
SELECT m.seq, CASE WHEN length(m.id) = 30 THEN m.id END,
       CASE WHEN m.type IN ({", ".join(repr(kind) for kind in _KINDS)}) THEN m.type END,
       {_COMPLETION}
FROM main.session_message AS m WHERE m.session_id = ?
ORDER BY m.seq DESC LIMIT 2
"""
_ORIGINAL_SQL = f"""
SELECT m.seq, {_COMPLETION}
FROM main.session_message AS m WHERE m.session_id = ? AND m.id = ? AND m.type = 'assistant'
LIMIT 2
"""
_WORK_SQL = f"""
SELECT count(*), max(state) FROM (
  SELECT CASE
    WHEN json_extract(m.data, '$.status') = 'running' THEN 1
    WHEN m.type = 'shell' AND json_extract(m.data, '$.status') IN ('exited', 'timeout', 'killed') THEN 0
    WHEN m.type = 'compaction' AND json_extract(m.data, '$.status') IN ('completed', 'failed') THEN 0
    ELSE 2
  END AS state
  FROM main.session_message AS m WHERE m.session_id = ? AND m.type IN ('shell', 'compaction')
  LIMIT {_MAX_COUNTS + 1}
)
"""
_JOBS_SQL = f"""
SELECT CASE WHEN j.kind = 'subagent' AND j.session_id = :parent
        AND j.child_session_id = :child AND j.id = :child
        AND json_extract(j.recovery, '$.kind') = 'subagent'
        AND json_extract(j.recovery, '$.childSessionID') = j.child_session_id
        AND json_extract(j.recovery, '$.parentSessionID') = j.session_id
        AND json_extract(j.recovery, '$.agent') IN (:agent, :agent_label)
        AND (
          json_type(j.recovery, '$.outstanding') IS NULL
          OR (json_type(j.recovery, '$.outstanding') = 'array'
            AND json_array_length(j.recovery, '$.outstanding') = 0)
        )
      THEN CASE WHEN length(j.notification_id) = 30 THEN j.notification_id END
      END,
      CASE WHEN j.status IN ('running', 'completed', 'error', 'cancelled') THEN j.status END
FROM main.job AS j
WHERE j.session_id = :child OR j.child_session_id = :child OR j.id = :child
LIMIT {_MAX_COUNTS + 1}
"""


def _boolean(value):
    _require(type(value) is int and value in (0, 1), "metadata-shape")
    return bool(value)


def _count(value):
    _require(type(value) is int and 0 <= value <= _MAX_COUNTS, "metadata-bound")
    return value


def _row(connection, directory, session, original):
    sessions = connection.execute(_SESSION_SQL, (directory, session)).fetchall()
    if not sessions:
        return None
    _require(len(sessions) == 1, "metadata-shape")
    parent, agent, *state, valid_times, children, inbox = sessions[0]
    tail = connection.execute(_TAIL_SQL, (session,)).fetchall()
    if not tail:
        return None
    sequence, last_id, last_kind, last_completed = tail[0]
    _require(
        _id(_SESSION_ID, parent)
        and agent in _AGENT_LABELS
        and _id(_MESSAGE_ID, last_id)
        and last_kind in _KINDS
        and type(sequence) is int
        and sequence >= 0
        and (len(tail) == 1 or type(tail[1][0]) is int and tail[1][0] < sequence)
        and _boolean(valid_times),
        "metadata-shape",
    )
    unfinished = last_kind == "assistant" and not _boolean(last_completed)
    if original is None:
        if not unfinished:
            return None
        original, original_completed = last_id, False
    else:
        originals = connection.execute(_ORIGINAL_SQL, (session, original)).fetchall()
        if not originals:
            return None
        _require(len(originals) == 1, "metadata-shape")
        earlier, completed = originals[0]
        _require(type(earlier) is int and 0 <= earlier <= sequence, "metadata-shape")
        original_completed = _boolean(completed)
    work_count, work = connection.execute(_WORK_SQL, (session,)).fetchone()
    _count(work_count)
    running_work = _boolean(work) if work_count else False
    jobs = connection.execute(
        _JOBS_SQL,
        {
            "parent": parent,
            "child": session,
            "agent": agent,
            "agent_label": _AGENT_LABELS[agent],
        },
    ).fetchall()
    job_count = _count(len(jobs))
    job = job_status = job_kind = None
    if job_count == 1:
        notification, status = jobs[0]
        if _id(_MESSAGE_ID, notification) and status in _JOB_STATUSES:
            job, job_status, job_kind = notification, status, "subagent"
    (
        claim,
        busy,
        compacting,
        archived,
        child_revert,
        parent_revert,
        parent_is_child,
        parent_archived,
    ) = (_boolean(flag) for flag in state)
    return {
        "session": session,
        "parent": parent,
        "agent": agent,
        "message": original,
        "tail_message": last_id,
        "last_kind": last_kind,
        "unfinished": unfinished,
        "original_completed": original_completed,
        "claim": claim,
        "busy": busy,
        "compacting": compacting,
        "archived": archived,
        "child_revert": child_revert,
        "parent_revert": parent_revert,
        "parent_is_child": parent_is_child,
        "parent_archived": parent_archived,
        "child_count": _count(children),
        "inbox_count": _count(inbox),
        "job_count": job_count,
        "shell_or_compaction_running": running_work,
        "job": job,
        "job_status": job_status,
        "job_kind": job_kind,
    }


def _rows(connection, directory, targets):
    directory = str(directory)
    if targets is not None:
        rows = [
            _row(connection, directory, target["session"], target["message"])
            for target in targets
        ]
        _require(all(row is not None for row in rows), "target-unavailable")
        return rows
    candidates = connection.execute(
        _DISCOVERY_SQL, (directory, _MAX_DISCOVERY + 1)
    ).fetchall()
    _require(len(candidates) <= _MAX_DISCOVERY, "metadata-bound")
    rows = []
    for (session,) in candidates:
        _require(_id(_SESSION_ID, session), "metadata-shape")
        row = _row(connection, directory, session, None)
        if (
            row is not None
            and row["job_status"] == "running"
            and not any(
                row[key]
                for key in (
                    "child_count",
                    "inbox_count",
                    "compacting",
                    "shell_or_compaction_running",
                )
            )
        ):
            rows.append(row)
            _require(len(rows) <= MAX_TARGETS, "metadata-bound")
    return rows


def _snapshot(sqlite, source, directory, targets, deadline):
    source.recheck()
    filename = source.db.as_uri() + "?mode=ro&vfs=unix&readonly_shm=1"
    with closing(
        sqlite.connect(
            filename,
            uri=True,
            timeout=0.05,
            isolation_level=None,
            cached_statements=0,
        )
    ) as connection:
        source.sqlite_fds(source.files, opening=True)
        if hasattr(connection, "setlimit"):
            connection.setlimit(sqlite.SQLITE_LIMIT_LENGTH, _MAX_SQL_VALUE)
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 256)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        _require(
            connection.execute("PRAGMA database_list").fetchall()
            == [(0, "main", str(source.db))]
            and connection.execute("PRAGMA main.journal_mode").fetchone() == ("wal",)
            and connection.execute("PRAGMA query_only").fetchone() == (1,)
            and connection.execute("PRAGMA trusted_schema").fetchone() == (0,),
            "sqlite-state",
        )
        connection.execute("SELECT 1 FROM main.sqlite_schema LIMIT 1").fetchall()
        database_fds = source.sqlite_fds(source.files)
        generation = "PRAGMA main.data_version"
        before = connection.execute(generation).fetchone()
        _require(before is not None and type(before[0]) is int, "sqlite-state")
        connection.execute("BEGIN")
        try:
            _probe_schema(connection)
            _require(
                connection.in_transaction
                and connection.execute(generation).fetchone() == before,
                "generation-changed",
            )
            connection.set_authorizer(_authorizer(sqlite))
            rows = _rows(connection, directory, targets)
        finally:
            # Keep the query fence in place through retirement. Python 3.10
            # does not support disabling an authorizer with None.
            connection.execute("ROLLBACK")
        source.recheck()
        _require(
            connection.execute(generation).fetchone() == before, "generation-changed"
        )
        _require(source.sqlite_fds(source.files) == database_fds, "sqlite-descriptors")
    source.sqlite_fds(())
    source.recheck()
    return rows


def _read(sqlite, directory, db, pid, targets, deadline):
    _require(sys.platform == "linux" and hasattr(os, "pidfd_open"), "platform")
    libc = ctypes.CDLL(None, use_errno=True)
    with ExitStack() as stack:
        proc = _open(stack, "/proc", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        # f_type is the leading ABI word of Linux statfs; leave the unused
        # fields in oversized, aligned storage.
        fs = (ctypes.c_long * 64)()
        _require(
            libc.fstatfs(proc, ctypes.byref(fs)) == 0
            and fs[0] == 0x9FA0
            and os.readlink("self", dir_fd=proc) == str(os.getpid()),
            "process-binding",
        )
        observer = _open(
            stack,
            str(os.getpid()),
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=proc,
        )
        # A Landlock domain cannot inspect an unsandboxed peer's proc links.
        # Bind the native handles before entering it; afterward keep the
        # pidfd live and recheck pinned storage and this reader's actual FDs.
        # The attached caller separately rechecks native readiness after this
        # one-shot observation.
        source = _Source(stack, proc, observer, directory, db, pid)
        source.recheck(native=True)
        _seal(libc, observer, source)
        initial, maximum = resource.getrlimit(resource.RLIMIT_AS)
        limit = (
            _MAX_ADDRESS_SPACE
            if initial == resource.RLIM_INFINITY
            else min(initial, _MAX_ADDRESS_SPACE)
        )
        resource.setrlimit(resource.RLIMIT_AS, (limit, maximum))
        return _snapshot(sqlite, source, directory, targets, deadline)


def _expired(_signum, _frame):
    raise _Refusal("deadline")


def _main():
    try:
        deadline = time.monotonic() + _DEADLINE
        signal.signal(signal.SIGALRM, _expired)
        signal.setitimer(signal.ITIMER_REAL, _DEADLINE)
        import sqlite3

        directory, db, pid, targets = _request()
        rows = _read(sqlite3, directory, db, pid, targets, deadline)
        _require(time.monotonic() < deadline, "deadline")
        print(json.dumps({"rows": rows}, separators=(",", ":")), flush=True)
        return 0
    except _Refusal as error:
        reason = error.args[0]
    except Exception:
        # This isolated process never sends SQLite/OS errors, tracebacks, or
        # individual rows to the host. A refusal is not an empty inventory.
        reason = (
            "deadline" if time.monotonic() >= deadline else "metadata-read-unavailable"
        )
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    try:
        print(json.dumps({"refused": reason}, separators=(",", ":")), flush=True)
    except OSError:
        pass
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
