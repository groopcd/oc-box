#!/usr/bin/env python3
"""Opt-in external HOST acceptance for a separate, unchanged oc-box candidate.

Run with -I -B from this harness checkout, not from --source. Only fresh private
synthetic fixtures are accepted. Reports/resources survive failures; this tool
does not offer retained retries or cleanup. No host acceptance is established by
its unit/native rehearsals. See docs/sharing-host-acceptance.md.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
from datetime import datetime
import errno
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import re
import resource
import select
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import termios
import threading
import time
import traceback
from types import SimpleNamespace
from unittest.mock import patch

FORMAT = "oc-box-sharing-host-v1"
MARKER = "sharing-purpose.json"
REPORT = "sharing-result.json"
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
PROJECTS = {"source-a", "source-b", "restored-full"}
INPUTS = {"share-input-a", "share-input-b"}
MAX_OUTPUT = 256 * 1024
CONTROL_FD_ENV = "OC_BOX_SHARING_CONTROL_FD"
OWNER_STAGES = frozenset(
    (
        "owner-source",
        "owner-validation",
        "owner-store",
        "owner-native",
        "owner-launch",
        "owner-ready",
        "owner-wait",
        "owner-finished",
        "source-recheck",
    )
)
TRACE_FRAME = re.compile(
    r"[A-Za-z0-9_.-]{1,80}\.py:(?:<[a-z]{1,12}>|[A-Za-z_][A-Za-z0-9_]{0,63}):[0-9]{1,6}"
)
TRACE_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
CHECKS = (
    "private_store_native_source",
    "consent_cancel",
    "noninteractive_refusal",
    "protected_source_refusal",
    "consent_accept_warning",
    "single_file_image",
    "live_list_text",
    "hidden_and_new_files",
    "atomic_child_update",
    "read_only_source",
    "traversal_link_special_refusals",
    "native_denial",
    "ordinary_shell_isolation",
    "ordinary_read_isolation",
    "two_shares",
    "two_workspaces",
    "cross_workspace_refusal",
    "no_mount_ports_config_refresh",
    "ctrl_c_revocation",
    "killed_client_revocation",
    "reload_revocation",
    "unrelated_mcp_preserved",
    "owner_waits_only_for_share",
    "lease_release_stops_owner",
    "resume_no_regrant",
    "explicit_stop_revocation",
    "reset_no_regrant",
    "backup_external_tree_excluded",
    "restore_fresh_stopped_identity",
    "restore_no_regrant",
    "source_and_ciphertext_preserved",
    "journal_private_bounded_no_contents",
    "owners_stopped",
    "unchanged_source_and_harness",
    "host_clean",
)
EXTRA_CHECKS = ("nested_mount_refusal", "enforcing_selinux_source_labels_unchanged")
UNRELATED_MCP = """import json,sys
for line in sys.stdin:
 item=json.loads(line)
 if 'id' not in item: continue
 method=item.get('method')
 if method=='initialize': value={'protocolVersion':item['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'unrelated','version':'1'}}
 elif method=='tools/list': value={'tools':[{'name':'check','description':'Synthetic unrelated MCP','inputSchema':{'type':'object','properties':{},'additionalProperties':False},'annotations':{'readOnlyHint':True}}]}
 elif method=='tools/call': value={'content':[{'type':'text','text':'UNRELATED_SHARING_MCP_OK'}]}
 else: value={}
 print(json.dumps({'jsonrpc':'2.0','id':item['id'],'result':value}),flush=True)
"""
CATEGORIES = frozenset(
    (
        "invalid-options",
        "host-only",
        "placement",
        "source-changed",
        "purpose",
        "native-changed",
        "private-store",
        "workspace-allowlist",
        "owner-start",
        "owner-exit",
        "share-start",
        "share-exit",
        "timeout",
        "bounded-output",
        "protocol",
        "oracle",
        "provider",
        "source-mutated",
        "pending-permission",
        "child-failed",
        "mount-probe",
        "backup",
        "journal",
        "io-error",
        "interrupted",
        "operation-failed",
        "refresh-called",
        "forced-owner-termination",
        "owner-control-protocol",
        "owner-control-eof",
        "owner-control-timeout",
        "missing-checks",
        "missing-host-scope",
    )
)


class Refusal(Exception):
    """Fixed report categories only; never native output, argv or file contents."""


class ChildControlFailure(Refusal):
    """A newly consumed validated child failure, not a local transport error."""


def require(condition, category="oracle"):
    if not condition:
        raise Refusal(category)


def digest(path):
    with Path(path).open("rb") as stream:
        value = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
        return value.hexdigest()


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec is not None and spec.loader is not None, "purpose")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def helpers():
    directory = Path(__file__).resolve().parent
    return (
        load_file(
            "_sharing_history_harness", directory / "test_session_sync_podman.py"
        ),
        directory / "sharing_fixture_provider.py",
    )


def harness_hashes():
    directory = Path(__file__).resolve().parent
    return {
        name: digest(directory / name)
        for name in (
            "test_sharing_podman.py",
            "test_session_sync_podman.py",
            "sharing_fixture_provider.py",
            "sharing_archive_scan.py",
        )
    }


def category(error):
    if (
        isinstance(error, Refusal)
        or type(error).__name__ in ("Refusal", "FixtureError")
    ) and str(error) in CATEGORIES:
        return str(error)
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return "interrupted"
    if isinstance(error, OSError):
        return "io-error"
    return "operation-failed"


def failure_trace(error):
    """Code identities only; never exception messages, locals or full paths."""
    result, seen = [], set()
    while error is not None and id(error) not in seen and len(result) < 4:
        seen.add(id(error))
        frames = [
            f"{Path(item.filename).name}:{item.name}:{item.lineno}"
            for item in traceback.extract_tb(error.__traceback__)[-6:]
        ]
        name = type(error).__name__
        result.append(
            {
                "type": name if TRACE_TYPE.fullmatch(name) else "other",
                "frames": [frame for frame in frames if TRACE_FRAME.fullmatch(frame)],
            }
        )
        error = error.__cause__ or error.__context__
    return result


def valid_trace(value):
    return (
        isinstance(value, list)
        and len(value) <= 4
        and all(
            isinstance(item, dict)
            and set(item) == {"type", "frames"}
            and isinstance(item["type"], str)
            and TRACE_TYPE.fullmatch(item["type"])
            and isinstance(item["frames"], list)
            and len(item["frames"]) <= 6
            and all(
                isinstance(frame, str) and TRACE_FRAME.fullmatch(frame)
                for frame in item["frames"]
            )
            for item in value
        )
    )


def owner_control():
    """A per-child inherited pipe, isolated from stdout and all native execs."""
    value = os.environ.pop(CONTROL_FD_ENV, "")
    require(re.fullmatch(r"[0-9]{1,10}", value) is not None, "owner-control-protocol")
    descriptor = int(value)
    require(3 <= descriptor <= 2**31 - 1, "owner-control-protocol")
    try:
        info = os.fstat(descriptor)
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
        require(
            stat.S_ISFIFO(info.st_mode) and flags & os.O_ACCMODE == os.O_WRONLY,
            "owner-control-protocol",
        )
        os.set_inheritable(descriptor, False)
        return os.fdopen(descriptor, "w", buffering=1)
    except OSError:
        raise Refusal("owner-control-protocol") from None


def owner_stage(control, stage):
    require(stage in OWNER_STAGES, "owner-control-protocol")
    if control is not None:
        print(json.dumps({"stage": stage}), file=control, flush=True)


def wait(check, *, timeout=30, label="timeout"):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        require(time.monotonic() < deadline, label)
        time.sleep(0.05)


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def sharing_session_payload(directory, permissions=None):
    """One fixed native session contract for host runs and native rehearsals.

    An explicit, content-free title prevents unrelated automatic title inference
    from consuming the strictly armed synthetic provider's tool plan.
    """
    require(Path(directory).is_absolute(), "protocol")
    return {
        "location": {"directory": str(directory)},
        "title": "Synthetic host-sharing check",
        "agent": "build",
        "model": {"providerID": "fixture", "id": "share-model"},
        "permissions": permissions
        if permissions is not None
        else [{"action": "*", "resource": "*", "effect": "allow"}],
    }


def source_manifest(directory):
    """No symlink following or special-file I/O; no atime preservation claim."""
    result = {}
    pending = [Path(directory)]
    while pending:
        path = pending.pop()
        info = path.lstat()
        label = None
        try:
            label = os.getxattr(path, "security.selinux", follow_symlinks=False).hex()
        except OSError as error:
            require(
                error.errno in (errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP),
                "io-error",
            )
        item = [
            info.st_mode,
            info.st_uid,
            info.st_gid,
            info.st_nlink,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            label,
        ]
        if stat.S_ISREG(info.st_mode):
            item.append(digest(path))
        elif stat.S_ISLNK(info.st_mode):
            item.append(os.readlink(path))
        elif stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())
        result[str(path.relative_to(directory))] = item
    return result


def listeners():
    values = set()
    for path in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        for line in path.read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == "0A":
                values.add((path.name, fields[1], fields[9]))
    return values


def validate_journal(directory):
    """Independent fixed-schema/privacy oracle for the real production writer."""
    info = directory.lstat()
    require(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o700,
        "journal",
    )
    allowed = {
        "activity.jsonl",
        "activity.jsonl.1",
        "activity.jsonl.2",
        "activity.jsonl.3",
        "activity.lock",
    }
    paths = list(directory.iterdir())
    require(
        {path.name for path in paths} <= allowed
        and {"activity.jsonl", "activity.lock"} <= {path.name for path in paths},
        "journal",
    )
    count = 0
    commands = {
        "start",
        "stop",
        "sync",
        "reset",
        "repair",
        "terminal",
        "web",
        "upgrade",
        "secrets",
        "credentials",
        "backup",
        "backup-info",
        "restore",
        "purge",
        "list",
        "distros",
        "status",
        "doctor",
        "completion",
        "tui",
        "sessions",
    }
    events = {
        "command.started",
        "command.finished",
        "command.failed",
        "tui.started",
        "tui.finished",
        "session.archived",
        "session.unarchived",
        "session.deleted",
        "session.favorited",
    }

    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, "journal")
            value[key] = item
        return value

    for path in paths:
        info = path.lstat()
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_size <= 256 * 1024,
            "journal",
        )
        raw = path.read_bytes()
        if path.name == "activity.lock":
            require(not raw, "journal")
            continue
        require(not raw or raw.endswith(b"\n"), "journal")
        require(
            all(
                marker not in raw
                for marker in (
                    b"synthetic-live",
                    b"synthetic-hidden",
                    b"synthetic-new",
                    b"share-input-",
                    b"SHARING_NEVER_READ_CONTENT_",
                    b"UNREAD_EXTERNAL_TREE_SENTINEL",
                )
            ),
            "journal",
        )
        for line in raw.splitlines():
            require(0 < len(line) <= 1024, "journal")
            try:
                value = json.loads(line, object_pairs_hook=pairs)
            except (ValueError, UnicodeError):
                raise Refusal("journal") from None
            require(
                isinstance(value, dict)
                and {"event", "timestamp"} <= value.keys()
                and value.keys()
                <= {
                    "timestamp",
                    "event",
                    "command",
                    "workspace",
                    "outcome",
                    "exit_code",
                    "duration_ms",
                },
                "journal",
            )
            require(
                isinstance(value["event"], str)
                and value["event"] in events
                and isinstance(value["timestamp"], str)
                and re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", value["timestamp"]
                ),
                "journal",
            )
            try:
                datetime.fromisoformat(value["timestamp"][:-1] + "+00:00")
            except ValueError:
                raise Refusal("journal") from None
            require(
                "command" not in value
                or (isinstance(value["command"], str) and value["command"] in commands),
                "journal",
            )
            require(
                "workspace" not in value
                or (
                    isinstance(value["workspace"], str)
                    and re.fullmatch(r"[0-9a-f]{32}", value["workspace"])
                ),
                "journal",
            )
            require(
                "outcome" not in value
                or isinstance(value["outcome"], str)
                and value["outcome"]
                in {"success", "failure", "cancelled", "refused", "unavailable"},
                "journal",
            )
            for key, low, high in (
                ("exit_code", -255, 255),
                ("duration_ms", 0, 2**53 - 1),
            ):
                require(
                    key not in value
                    or (type(value[key]) is int and low <= value[key] <= high),
                    "journal",
                )
            count += 1
    require(count > 0, "journal")
    return count


def mount_at(path):
    matches = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        fields = left.split()
        target = re.sub(r"\\([0-7]{3})", lambda item: chr(int(item[1], 8)), fields[4])
        if target == str(path):
            matches.append((fields[0], right.split()[0]))
    require(len(matches) <= 1, "mount-probe")
    return matches[0] if matches else None


class NestedMount:
    """Only a fresh synthetic user FUSE mount, never privileged host mounting."""

    def __init__(self, source, target):
        self.source, self.target = Path(source), Path(target)
        self.process = self.errors = self.mounted = None

    def __enter__(self):
        require(
            mount_at(self.target) is None
            and self.target.is_dir()
            and not self.target.is_symlink()
            and self.source.is_dir()
            and not self.source.is_symlink(),
            "mount-probe",
        )
        binary, unmount = shutil.which("bindfs"), shutil.which("fusermount3")
        require(binary is not None and unmount is not None, "mount-probe")
        self.unmount = unmount
        self.errors = os.fdopen(
            os.memfd_create("sharing-fuse-errors", os.MFD_CLOEXEC), "w+b"
        )
        self.process = subprocess.Popen(
            [
                binary,
                "-f",
                "-r",
                "--no-allow-other",
                str(self.source),
                str(self.target),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=self.errors,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"},
            start_new_session=True,
        )
        try:

            def ready():
                require(self.process.poll() is None, "mount-probe")
                found = mount_at(self.target)
                if found:
                    require(found[1] in ("fuse.bindfs", "fuse"), "mount-probe")
                return found

            self.mounted = wait(ready, timeout=15, label="mount-probe")
            require(
                (self.target / "mounted.txt").read_text() == "MOUNTED_NOT_EXPOSED",
                "mount-probe",
            )
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.mounted is not None:
            require(mount_at(self.target) == self.mounted, "mount-probe")
            result = subprocess.run(
                [self.unmount, "-u", str(self.target)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={"PATH": os.defpath, "LANG": "C.UTF-8"},
                timeout=15,
            )
            require(
                result.returncode == 0 and mount_at(self.target) is None, "mount-probe"
            )
            self.mounted = None
        if self.process is not None:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=5)
        if self.errors is not None:
            self.errors.close()

    def __exit__(self, *_):
        self.close()


class PipeChild:
    """Dedicated JSON control pipe; stdout/stderr are anonymous private memory."""

    def __init__(
        self, command, env, cwd, *, require_terminal=False, expected_stop=False
    ):
        self.require_terminal = require_terminal
        self.expected_stop = expected_stop
        self.errors = os.fdopen(
            os.memfd_create("sharing-fixture-child-errors", os.MFD_CLOEXEC), "w+b"
        )
        read_fd, write_fd = os.pipe()
        try:
            self.process = subprocess.Popen(
                command,
                env={**env, CONTROL_FD_ENV: str(write_fd)},
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=self.errors,
                stderr=self.errors,
                start_new_session=True,
                pass_fds=(write_fd,),
            )
        except BaseException:
            os.close(read_fd)
            self.errors.close()
            raise
        finally:
            os.close(write_fd)
        self.control = os.fdopen(read_fd, "rb", buffering=0)
        self.incoming = bytearray()
        self.last_stage = None
        self.child_failure = None
        self.control_category = None
        self.premature_wait_clients = None
        self.terminal = None

    def ready(self, timeout=1800):
        try:
            deadline = time.monotonic() + timeout
            while True:
                value = self.message(max(0, deadline - time.monotonic()))
                if value == {"ready": True}:
                    return
                if (
                    set(value) == {"wait_clients"}
                    and type(value["wait_clients"]) is int
                    and value["wait_clients"] >= 0
                ):
                    self.premature_wait_clients = value["wait_clients"]
                    continue  # Preserve the following structured startup failure.
                raise Refusal("protocol")
        except Refusal as error:
            self.control_category = category(error)
            raise

    def message(self, timeout=30, *, eof_ok=False):
        deadline = time.monotonic() + timeout
        try:
            while True:
                while b"\n" not in self.incoming:
                    require(time.monotonic() < deadline, "owner-control-timeout")
                    # Read buffered terminal/error frames even after child exit.
                    # CLOEXEC on the child endpoint prevents native descendants
                    # from falsely keeping this control pipe alive.
                    if select.select(
                        [self.control],
                        [],
                        [],
                        min(0.1, max(0, deadline - time.monotonic())),
                    )[0]:
                        content = os.read(self.control.fileno(), 4096)
                        if not content and eof_ok and not self.incoming:
                            return None
                        require(bool(content), "owner-control-eof")
                        self.incoming.extend(content)
                        require(len(self.incoming) <= 8192, "bounded-output")
                line, _, self.incoming = self.incoming.partition(b"\n")
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError):
                    raise Refusal("owner-control-protocol") from None
                require(isinstance(value, dict), "owner-control-protocol")
                if set(value) == {"stage"}:
                    require(
                        isinstance(value["stage"], str)
                        and value["stage"] in OWNER_STAGES,
                        "owner-control-protocol",
                    )
                    self.last_stage = value["stage"]
                    continue
                if set(value) == {"error", "failure_trace"}:
                    require(
                        isinstance(value["error"], str)
                        and value["error"] in CATEGORIES
                        and valid_trace(value["failure_trace"]),
                        "owner-control-protocol",
                    )
                    self.child_failure = value
                    raise ChildControlFailure(value["error"])
                if set(value) == {"owner_exit", "intentional_stop"}:
                    require(
                        self.terminal is None
                        and type(value["owner_exit"]) is int
                        and value["owner_exit"] in (0, 1)
                        and type(value["intentional_stop"]) is bool,
                        "owner-control-protocol",
                    )
                    self.terminal = value
                    return value
                require(
                    (set(value) == {"ready"} and value["ready"] is True)
                    or (
                        set(value) == {"wait_clients"}
                        and type(value["wait_clients"]) is int
                        and 0 <= value["wait_clients"] <= 2**31 - 1
                    ),
                    "owner-control-protocol",
                )
                return value
        except Refusal as error:
            self.control_category = category(error)
            raise

    def diagnostic(self):
        return {
            "last_stage": self.last_stage,
            "control_category": self.control_category,
            "child_failure": self.child_failure,
            "premature_wait_clients": self.premature_wait_clients,
            "terminal": self.terminal,
        }

    def release(self):
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()

    def drain(self, timeout=5):
        deadline = time.monotonic() + timeout
        while True:
            try:
                value = self.message(max(0, deadline - time.monotonic()), eof_ok=True)
                if value is None:
                    return
            except ChildControlFailure:
                continue  # Only new child frames are drainable; deadlines are not.

    def validate_terminal(self):
        if self.child_failure is not None:
            raise Refusal(self.child_failure["error"])
        if self.control_category is not None:
            raise Refusal(self.control_category)
        if self.require_terminal:
            require(
                self.terminal is not None
                and self.terminal["owner_exit"] == self.process.returncode
                and self.terminal["intentional_stop"] is self.expected_stop,
                "owner-control-protocol",
            )
            require(
                self.process.returncode == 0
                or self.expected_stop
                and self.process.returncode == 1,
                "owner-exit",
            )

    def finish(self, timeout=180):
        self.release()
        code = self.process.wait(timeout=timeout)
        known_failure = self.child_failure
        self.drain()
        if self.require_terminal:
            self.validate_terminal()
        else:
            if self.child_failure is not None and self.child_failure != known_failure:
                raise Refusal(self.child_failure["error"])
            require(code == 0, "owner-exit")
        return code

    def close(self, timeout=15):
        self.release()
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.forced = True
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=5)
        try:
            if not self.control.closed:
                try:
                    self.drain(timeout=2)
                except Refusal:
                    pass  # The diagnostic is retained; cleanup must still close FDs.
        finally:
            self.control.close()
            self.errors.close()


class ShareChild:
    """Real host controlling PTY for production consent, Ctrl-C and termination."""

    def __init__(self, command, env, cwd, *, tty=True):
        self.tty = tty
        self.content = bytearray()
        self.failure = None
        self.name = None
        self.done = threading.Event()
        self.condition = threading.Condition()
        if tty:
            self.fd, slave = pty.openpty()
            try:
                self.process = subprocess.Popen(
                    command,
                    env=env,
                    cwd=cwd,
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    start_new_session=True,
                )
            except BaseException:
                os.close(self.fd)
                raise
            finally:
                os.close(slave)
        else:
            read_fd, write_fd = os.pipe()
            self.fd = read_fd
            try:
                self.process = subprocess.Popen(
                    command,
                    env=env,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=write_fd,
                    stderr=write_fd,
                    start_new_session=True,
                )
            except BaseException:
                os.close(read_fd)
                raise
            finally:
                os.close(write_fd)
        self.worker = threading.Thread(target=self._read, daemon=True)
        self.worker.start()

    def _read(self):
        try:
            while not self.done.is_set():
                if not select.select([self.fd], [], [], 0.1)[0]:
                    continue
                try:
                    chunk = os.read(self.fd, 4096)
                except OSError as error:
                    if self.tty and error.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                with self.condition:
                    require(
                        len(self.content) + len(chunk) <= MAX_OUTPUT, "bounded-output"
                    )
                    self.content.extend(chunk)
                    require(
                        b"SHARING_FORBIDDEN_REFRESH_CALLED" not in self.content,
                        "refresh-called",
                    )
                    match = re.search(
                        rb"Live read-only tools: (oc_box_share_[0-9a-f]{32})\. ",
                        self.content,
                    )
                    if match:
                        self.name = match[1].decode("ascii")
                    self.condition.notify_all()
        except BaseException as error:
            self.failure = category(error)
        finally:
            self.done.set()
            with self.condition:
                self.condition.notify_all()

    def until(self, text=None, *, ready=False, timeout=45):
        deadline = time.monotonic() + timeout
        with self.condition:
            while not (self.name if ready else text in self.content):
                require(not self.failure and not self.done.is_set(), "share-start")
                require(time.monotonic() < deadline, "share-start")
                self.condition.wait(0.1)
        return self.name

    def send(self, value):
        require(self.tty and len(value) <= 1024, "protocol")
        require(os.write(self.fd, value) == len(value), "protocol")

    def finish(self, expected=0, timeout=45):
        require(self.process.wait(timeout=timeout) == expected, "share-exit")
        self.worker.join(3)
        require(not self.worker.is_alive() and not self.failure, "share-exit")

    def close(self):
        if self.process.poll() is None:
            os.kill(self.process.pid, signal.SIGINT)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.kill(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        self.done.set()
        self.worker.join(3)
        os.close(self.fd)


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "sharing fixture: category=invalid-options\n")


def parser():
    value = ArgumentParser(description=__doc__)
    value.add_argument("--source", type=Path, required=True)
    value.add_argument("--expect-commit", required=True)
    value.add_argument("--work-dir", type=Path)
    value.add_argument("--temp-dir", type=Path)
    value.add_argument("--opencode-v2", type=Path)
    value.add_argument("--distro", default="tumbleweed")
    value.add_argument(
        "--require-selinux",
        action="store_true",
        help="Refuse before setup unless SELinux is enforcing; otherwise report that scope separately.",
    )
    value.add_argument(
        "--mount-probe",
        action="store_true",
        help="Use installed bindfs/fusermount3 for a fresh synthetic nested FUSE mount; never install tools automatically.",
    )
    value.add_argument("--child", choices=("owner", "share"), help=argparse.SUPPRESS)
    value.add_argument("--fixture", type=Path, help=argparse.SUPPRESS)
    value.add_argument(
        "--project-name", choices=sorted(PROJECTS), help=argparse.SUPPRESS
    )
    value.add_argument("--input-name", choices=sorted(INPUTS), help=argparse.SUPPRESS)
    value.add_argument("--single-file", action="store_true", help=argparse.SUPPRESS)
    value.add_argument(
        "--acknowledge-risk", action="store_true", help=argparse.SUPPRESS
    )
    value.add_argument("--reset", action="store_true", help=argparse.SUPPRESS)
    value.add_argument("--expect-stop", action="store_true", help=argparse.SUPPRESS)
    value.add_argument("--protected", action="store_true", help=argparse.SUPPRESS)
    return value


def validate_options(args):
    require(
        re.fullmatch(r"[0-9a-f]{40}", args.expect_commit or "") is not None,
        "invalid-options",
    )
    if args.child:
        require(
            args.fixture is not None
            and args.project_name is not None
            and args.work_dir is None
            and args.temp_dir is None
            and args.opencode_v2 is None
            and not args.mount_probe
            and not args.require_selinux,
            "invalid-options",
        )
        require(
            (
                args.child == "owner"
                and args.input_name is None
                and not args.single_file
                and not args.acknowledge_risk
                and not args.protected
            )
            or (
                args.child == "share"
                and args.input_name in INPUTS
                and not args.reset
                and not args.expect_stop
            ),
            "invalid-options",
        )
    else:
        require(
            args.work_dir is not None
            and args.opencode_v2 is not None
            and all(
                getattr(args, name) is None
                for name in ("fixture", "project_name", "input_name")
            )
            and not any(
                (
                    args.single_file,
                    args.acknowledge_risk,
                    args.reset,
                    args.protected,
                    args.expect_stop,
                )
            ),
            "invalid-options",
        )


def load(args):
    history, provider_path = helpers()
    history.host_guard()  # Never unset/override container markers to get here.
    require(sys.flags.isolated and sys.dont_write_bytecode, "invalid-options")
    require(
        not Path(__file__).resolve().is_relative_to(args.source.expanduser().resolve()),
        "placement",
    )
    loaded = history.load_source(args.source, args.expect_commit)
    provider = load_file("_sharing_provider", provider_path)
    return history, loaded, provider


def purpose(root, loaded, runner, binary, config):
    return {
        "format": FORMAT,
        "root": str(root),
        "uid": os.getuid(),
        "source_identity": loaded.identity,
        "harness_sha256": runner,
        "native": {"path": str(binary), "version": "2.2.6", "sha256": digest(binary)},
        "config_sha256": hashlib.sha256(
            json.dumps(config, sort_keys=True).encode()
        ).hexdigest(),
    }


def fixture_environment(f, root):
    environment = f.isolated_env(root)
    config = f.private_json(root, "config/opencode/opencode.json")
    address = (
        config.get("providers", {})
        .get("fixture", {})
        .get("settings", {})
        .get("baseURL")
    )
    match = re.fullmatch(
        r"http://host\.containers\.internal:([0-9]{1,5})/v1", address or ""
    )
    require(match is not None and 0 < int(match[1]) < 65536, "purpose")
    environment["LEMONADE_BASE_URL"] = (
        "http://host.containers.internal:" + match[1] + "/api/v1"
    )
    return environment


def validate_root(root, loaded, *, initial=False):
    f = loaded.fixture
    root = f.validate_fixture(root, allow_unrecorded=True)
    # Internal children belong only to the current fresh run. Completed or
    # failed reports are immutable evidence, never authority for a replay.
    require(
        not any(
            (root / name).is_symlink() or (root / name).exists()
            for name in (REPORT, "result.json")
        ),
        "purpose",
    )
    marker = f.private_json(root, MARKER)
    require(
        isinstance(marker, dict)
        and set(marker)
        == {
            "format",
            "root",
            "uid",
            "source_identity",
            "harness_sha256",
            "native",
            "config_sha256",
        },
        "purpose",
    )
    require(
        marker["format"] == FORMAT
        and marker["root"] == str(root)
        and marker["uid"] == os.getuid()
        and marker["source_identity"] == loaded.identity
        and marker["harness_sha256"] == harness_hashes(),
        "purpose",
    )
    require(
        set(marker["native"]) == {"path", "version", "sha256"}
        and marker["native"]["version"] == "2.2.6",
        "native-changed",
    )
    binary = loaded.interactive.native_binary(Path(marker["native"]["path"]))
    require(digest(binary) == marker["native"]["sha256"], "native-changed")
    require(
        f.private_json(root, "executables.json") == {"2": str(binary)}, "native-changed"
    )
    require(
        (root / "bin/opencode").read_text() == f.native_wrapper_source(),
        "native-changed",
    )
    config = f.private_json(root, "config/opencode/opencode.json")
    require(
        hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        == marker["config_sha256"],
        "purpose",
    )
    records = f.workspace_records(root)
    require(
        set(records) <= PROJECTS
        and all(
            (
                "credentials" not in data
                if name != "restored-full"
                else data.get("credentials")
                == {"mode": "vault", "env": [], "vault": []}
            )
            for name, (_, data) in records.items()
        ),
        "workspace-allowlist",
    )
    require(not f.vault.vault_path().exists(), "purpose")
    f.require_runtime_boot(root, allow_uninitialized=initial and not records)
    return root, records


def entrypoint(box, arguments):
    previous = sys.argv
    try:
        sys.argv = ["oc-box", *arguments]
        return box.entrypoint()
    finally:
        sys.argv = previous


@contextlib.contextmanager
def observe_refresh(box):
    """Transparent call observations; no result, refusal or side effect is faked."""
    calls = []
    with contextlib.ExitStack() as stack:
        for name in (
            "prepare_runtime",
            "launch_credentials",
            "config_archive",
            "ensure_opencode_version",
            "start_container",
            "create_container",
            "sync_sessions",
        ):
            original = getattr(box, name)

            def forward(*args, _name=name, _original=original, **kwargs):
                calls.append(_name)
                print("SHARING_FORBIDDEN_REFRESH_CALLED", flush=True)
                return _original(*args, **kwargs)

            stack.enter_context(patch.object(box, name, forward))
        yield calls
    require(not calls, "refresh-called")


def child(args, loaded, *, control=None):
    """All production operations stay real; only owner TUI input is scripted."""
    f, box = loaded.fixture, loaded.box
    code = 0
    owner_stage(control, "owner-validation")
    if args.child == "share" and os.isatty(0):
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    with f.private_output():
        root = f.no_symlinks(args.fixture.expanduser()).resolve(strict=True)
        with (
            patch.dict(os.environ, fixture_environment(f, root), clear=True),
            f.temporary_location(root),
        ):
            _, records = validate_root(root, loaded)
            owner_stage(control, "owner-store")
            f.validate_store(root, allow_unrecorded=True)
            binary = Path(f.private_json(root, MARKER)["native"]["path"])
            owner_stage(control, "owner-native")
            f.check_native_version(root, binary, "2")
    # private_output must not hide a genuine share disclosure/confirmation PTY.
    with (
        patch.dict(os.environ, fixture_environment(f, root), clear=True),
        f.temporary_location(root),
    ):
        if args.child == "share":
            require(args.project_name in records, "workspace-allowlist")
            path, data = records[args.project_name]
            with f.private_output():
                f.verify_owned_resources(root, data)
                require(loaded.service.controller_running(box, data), "share-start")
            source = root / args.input_name
            if args.single_file:
                source /= "pixel.png"
            if args.protected:
                source = root / "registry"
            arguments = ["share", str(source), "--workspace", data["id"]]
            if args.acknowledge_risk:
                arguments.append("--acknowledge-risk")
            with f.no_vault_probe(), observe_refresh(box):
                code = entrypoint(box, arguments)
            require(code == 0, "share-exit")
        else:
            require(control is not None, "owner-control-protocol")
            channel = control
            try:
                original_wait = loaded.service.Owner.wait_clients
                ready_announced = False

                def wait_clients(owner):
                    owner_stage(control, "owner-wait")
                    print(
                        json.dumps({"wait_clients": owner.clients}),
                        file=channel,
                        flush=True,
                    )
                    return original_wait(owner)

                def callback(command):
                    nonlocal ready_announced
                    records = f.workspace_records(root)
                    require(args.project_name in records, "workspace-allowlist")
                    path, data = records[args.project_name]
                    f.verify_owned_resources(root, data)
                    f.selected_session(command, path)
                    require(loaded.service.live_connection() is not None, "owner-start")
                    f.record_ids(root)
                    owner_stage(control, "owner-ready")
                    print('{"ready":true}', file=channel, flush=True)
                    ready_announced = True
                    require(sys.stdin.buffer.read(1) == b"", "protocol")
                    return 0

                with (
                    f.private_output(),
                    f.no_vault_probe(),
                    patch.object(box, "run_tui", callback),
                    patch.object(loaded.service.Owner, "wait_clients", wait_clients),
                ):
                    operation = "reset" if args.reset else "start"
                    owner_stage(control, "owner-launch")
                    code = entrypoint(
                        box,
                        [
                            operation,
                            "--project",
                            str(root / args.project_name),
                            "--distro",
                            args.distro,
                            "--no-vault",
                        ],
                    )
                    if code != 0:
                        sys.stderr.flush()
                        descriptor = sys.stderr.fileno()
                        size = os.fstat(descriptor).st_size
                        tail = os.pread(
                            descriptor, MAX_OUTPUT, max(0, size - MAX_OUTPUT)
                        )
                        require(
                            args.expect_stop
                            and ready_announced
                            and code == 1
                            and sum(
                                line.startswith(b"oc-box: history sync pending: ")
                                for line in tail.splitlines()
                            )
                            == 1,
                            "owner-exit",
                        )
                    records = f.workspace_records(root)
                    f.verify_owned_resources(
                        root, records[args.project_name][1], stopped=True
                    )
                    f.record_ids(root)
                    owner_stage(control, "owner-finished")
            finally:
                channel.flush()
    owner_stage(control, "source-recheck")
    require(loaded.history_identity() == loaded.identity, "source-changed")
    if control is not None:
        print(
            json.dumps({"owner_exit": code, "intentional_stop": args.expect_stop}),
            file=control,
            flush=True,
        )
    return code


class Harness:
    def __init__(self, args, history, loaded, provider, root):
        self.args, self.history, self.loaded = args, history, loaded
        self.f, self.box, self.service = loaded.fixture, loaded.box, loaded.service
        self.provider, self.root = provider, root
        self.runner = harness_hashes()
        self.checks, self.unavailable, self.secondary, self.exits = {}, [], [], []
        self.owners, self.shares, self.names = [], [], []
        self.stage = "preparation"
        self.expected_inputs = {}
        self.active_mount = None
        self.native = {
            "version": "2.2.6",
            "sha256": digest(args.opencode_v2.resolve(strict=True)),
        }

    def check(self, name):
        require(name in (*CHECKS, *EXTRA_CHECKS), "protocol")
        self.checks[name] = True
        self.progress(name)

    def progress(self, name):
        print("sharing fixture: " + name, file=self.output, flush=True)

    def same_source(self):
        require(
            self.history.source_snapshot(self.loaded.source, self.args.expect_commit)
            == self.loaded.identity,
            "source-changed",
        )
        require(harness_hashes() == self.runner, "source-changed")

    def validate(self, *, initial=False):
        self.same_source()
        _, records = validate_root(self.root, self.loaded, initial=initial)
        self.f.validate_store(self.root, allow_unrecorded=True)
        return records

    def capture_inputs(self):
        require(not self.expected_inputs, "oracle")
        self.expected_inputs = {
            name: source_manifest(self.root / name) for name in INPUTS
        }

    def verify_inputs(self):
        require(set(self.expected_inputs) == INPUTS, "oracle")
        for name, expected in self.expected_inputs.items():
            actual = source_manifest(self.root / name)
            if self.active_mount is not None and name == "share-input-a":
                require(
                    mount_at(self.active_mount.target) == self.active_mount.mounted,
                    "mount-probe",
                )

                def outside_mount(values):
                    return {
                        key: value
                        for key, value in values.items()
                        if key != "nested-mount" and not key.startswith("nested-mount/")
                    }

                expected, actual = outside_mount(expected), outside_mount(actual)
            require(actual == expected, "source-mutated")

    def change_input(self, name, filename, content, *, replace=False):
        """Only a declared fixture edit can advance the expected source state."""
        self.verify_inputs()
        expected = self.expected_inputs[name]
        destination = self.root / name / filename
        if replace:
            temporary = self.root / "replacement.txt"
            temporary.write_text(content)
            temporary.replace(destination)
        else:
            require(not destination.exists(), "oracle")
            destination.write_text(content)
        actual = source_manifest(self.root / name)
        require(
            destination.read_text() == content
            and set(actual) == set(expected) | {filename},
            "source-mutated",
        )
        for key in set(actual) - {filename}:
            if key == ".":
                # Directory size/mtime/ctime change on an intentional child edit;
                # mode, UID/GID, link count and security label must not change.
                require(
                    actual[key][:4] == expected[key][:4]
                    and actual[key][7:] == expected[key][7:],
                    "source-mutated",
                )
            else:
                require(actual[key] == expected[key], "source-mutated")
        self.expected_inputs[name] = actual
        self.verify_inputs()

    def command(self, mode, project, options=()):
        return [
            sys.executable,
            "-I",
            "-B",
            str(Path(__file__).resolve()),
            "--source",
            str(self.loaded.source),
            "--expect-commit",
            self.args.expect_commit,
            "--child",
            mode,
            "--fixture",
            str(self.root),
            "--project-name",
            project,
            "--distro",
            self.args.distro,
            *options,
        ]

    def owner(self, project="source-a", *, reset=False, expect_stop=False):
        self.verify_inputs()
        self.validate(initial=not self.f.workspace_records(self.root))
        options = ["--reset"] if reset else []
        if expect_stop:
            options.append("--expect-stop")
        child = PipeChild(
            self.command("owner", project, options),
            fixture_environment(self.f, self.root),
            self.loaded.source,
            require_terminal=True,
            expected_stop=expect_stop,
        )
        self.owners.append(child)
        child.ready()
        self.f.record_ids(self.root)
        self.validate()
        return child

    def share(
        self,
        project="source-a",
        input_name="share-input-a",
        *,
        ack=False,
        single=False,
        protected=False,
        tty=True,
    ):
        self.verify_inputs()
        self.validate()
        options = ["--input-name", input_name]
        if ack:
            options.append("--acknowledge-risk")
        if single:
            options.append("--single-file")
        if protected:
            options.append("--protected")
        child = ShareChild(
            self.command("share", project, options),
            fixture_environment(self.f, self.root),
            self.loaded.source,
            tty=tty,
        )
        self.shares.append(child)
        return child

    def accepted_share(self, *args, **kwargs):
        child = self.share(*args, **kwargs)
        if not kwargs.get("ack", False):
            child.until(b"Type 'share'")
            require(
                b"SECURITY WARNING" in child.content
                and b"cannot recall" in child.content,
                "oracle",
            )
            require(child.name is None, "oracle")
            child.send(b"share\n")
        name = child.until(ready=True)
        self.verify_inputs()
        self.names.append(name)
        return child, name

    @contextlib.contextmanager
    def connection(self, project="source-a"):
        # File-only retained-boot/purpose checks precede every engine access.
        # Full source/store revalidation is repeated at phase boundaries; the
        # production attach validates current project/mount/owner state itself.
        _, records = validate_root(self.root, self.loaded)
        path, data = records[project]
        with self.service.attach(self.box, path, data) as connection:
            yield data, connection

    def request(self, method, route, value=None, *, project="source-a"):
        with self.connection(project) as (data, connection):
            script = (
                "import json,sys\nnamespace={'__name__':'fixture_bridge'}\nexec("
                + repr((self.box.PACKAGE / "opencode_v2_io.py").read_text())
                + ",namespace)\nsettings=json.load(sys.stdin)\nrequest=namespace['endpoint_request'](settings['endpoint'])\nprint(json.dumps(request(*settings['operation'])))\n"
            )
            command = self.service.client_command(
                self.box, data, connection, ["python3", "-c", script]
            )
            output = self.box.run(
                command,
                input=json.dumps(
                    {
                        "endpoint": {
                            key: connection[key] for key in ("url", "password")
                        },
                        "operation": [method, route, value],
                    }
                ),
                capture=True,
                private=True,
                timeout=90,
            ).stdout
            return json.loads(output)

    def container_python(self, code, payload=None, *, project="source-a"):
        with self.connection(project) as (data, connection):
            command = self.service.client_command(
                self.box, data, connection, ["python3", "-c", code]
            )
            result = self.box.run(
                command,
                input=json.dumps(payload),
                capture=True,
                private=True,
                timeout=60,
            )
            return json.loads(result.stdout)

    def invoke(self, action, arguments, *, project="source-a", permissions=None):
        self.verify_inputs()
        token = self.provider.plan(action, arguments)
        records = self.f.workspace_records(self.root)
        directory = self.box.container_project(records[project][1])
        value = sharing_session_payload(directory, permissions)
        ident = self.request("POST", "/api/session", value, project=project)["data"][
            "id"
        ]
        self.last_session = ident
        self.request(
            "POST",
            "/api/session/" + ident + "/prompt",
            {"text": token},
            project=project,
        )

        def settled():
            require(self.provider.errors == 0, "provider")
            messages = self.request(
                "GET",
                "/api/session/" + ident + "/message?limit=100&order=asc",
                project=project,
            )["data"]
            active = self.request("GET", "/api/session/active", project=project)["data"]
            return (
                messages
                if ident not in active
                and any(
                    item.get("type") == "assistant"
                    and item.get("time", {}).get("completed")
                    for item in messages
                )
                else None
            )

        messages = wait(settled, timeout=90)
        captured = wait(lambda: self.provider.take(token), timeout=10, label="provider")
        self.verify_inputs()
        return captured, messages

    def call(self, name, tool, path=".", *, project="source-a", permissions=None):
        return self.invoke(
            "execute",
            {"code": f"return await tools.{name}.{tool}({json.dumps({'path': path})})"},
            project=project,
            permissions=permissions,
        )

    @staticmethod
    def text(result):
        require(
            isinstance(result, dict) and isinstance(result.get("text"), str), "provider"
        )
        return result["text"]

    def unknown(self, name, *, project="source-a"):
        # Native catalog removal is asynchronous. Observe the actual namespace,
        # not owner liveness, and permit one or two transient stale observations.
        for attempt in range(3):
            result, _ = self.call(name, "read_file", "text.txt", project=project)
            text = self.text(result)
            if "Unknown tool" in text and name + ".read_file" in text:
                require("synthetic-live" not in text, "oracle")
                return
            if attempt < 2:
                time.sleep(0.1)
        raise Refusal("oracle")

    def no_regrant(self, project):
        for name in self.names:
            self.unknown(name, project=project)

    def read_unchanged(
        self, name, expected, *, project="source-a", input_name="share-input-a"
    ):
        before = source_manifest(self.root / input_name)
        result, _ = self.call(name, "read_file", "text.txt", project=project)
        require(self.text(result) == expected, "oracle")
        require(source_manifest(self.root / input_name) == before, "source-mutated")

    def stable_config(self, project="source-a"):
        records = self.f.workspace_records(self.root)
        path, data = records[project]
        config = {
            str(item.relative_to(self.root)): digest(item)
            for base in (self.root / "config", path)
            for item in ([base] if base.is_file() else base.rglob("*"))
            if item.is_file() and not item.is_symlink()
        }
        inspection = self.f.verify_owned_resources(self.root, data)
        code = """import hashlib,json,os,pathlib,stat,sys
root=pathlib.Path(json.load(sys.stdin)); pending=[root]; items=[]
while pending:
 path=pending.pop(); info=path.lstat(); value=[str(path.relative_to(root)),info.st_mode,info.st_uid,info.st_gid,info.st_size,info.st_mtime_ns,info.st_ctime_ns]
 if stat.S_ISDIR(info.st_mode): pending.extend(path.iterdir())
 elif stat.S_ISREG(info.st_mode): value.append(hashlib.sha256(path.read_bytes()).hexdigest())
 elif stat.S_ISLNK(info.st_mode): value.append(os.readlink(path))
 items.append(value)
print(json.dumps(hashlib.sha256(json.dumps(sorted(items)).encode()).hexdigest()))
"""
        container_config = self.container_python(
            code, data["home"] + "/.config/opencode", project=project
        )
        credentials = self.request("GET", "/api/credential", project=project)
        return (
            config,
            inspection.get("Mounts"),
            inspection.get("HostConfig", {}).get("PortBindings"),
            listeners(),
            container_config,
            hashlib.sha256(
                json.dumps(credentials, sort_keys=True).encode()
            ).hexdigest(),
        )

    def core(self):
        self.stage = "live-sharing"
        self.verify_inputs()
        owner = self.owner()
        self.check("private_store_native_source")
        cancelled = self.share()
        cancelled.until(b"Type 'share'")
        cancelled.send(b"no\n")
        cancelled.finish(1)
        require(cancelled.name is None and b"SECURITY WARNING" in cancelled.content)
        self.check("consent_cancel")
        noninteractive = self.share(tty=False)
        noninteractive.finish(1)
        require(noninteractive.name is None)
        self.check("noninteractive_refusal")
        protected = self.share(protected=True, ack=True)
        protected.finish(1)
        require(protected.name is None)
        self.check("protected_source_refusal")
        initial = self.stable_config()
        shared, name = self.accepted_share()
        require(self.stable_config() == initial, "oracle")
        self.check("consent_accept_warning")
        self.check("no_mount_ports_config_refresh")
        self.read_unchanged(name, "synthetic-live-before 中")
        listing, _ = self.call(name, "list_directory")
        require(".hidden" in self.text(listing) and "text.txt" in self.text(listing))
        self.check("live_list_text")
        self.read_unchanged(name, "synthetic-live-before 中")
        hidden, _ = self.call(name, "read_file", ".hidden")
        require(self.text(hidden) == "synthetic-hidden")
        self.change_input("share-input-a", "new.txt", "synthetic-new")
        fresh, _ = self.call(name, "read_file", "new.txt")
        require(self.text(fresh) == "synthetic-new")
        self.check("hidden_and_new_files")
        self.change_input(
            "share-input-a", "text.txt", "synthetic-live-after 🙂", replace=True
        )
        self.read_unchanged(name, "synthetic-live-after 🙂")
        self.check("atomic_child_update")
        mutation, _ = self.call(name, "write_file", "text.txt")
        require("Unknown tool" in self.text(mutation), "oracle")
        self.read_unchanged(name, "synthetic-live-after 🙂")
        self.check("read_only_source")
        before = source_manifest(self.root / "share-input-a")
        for path in (
            "../outside-not-shared.txt",
            "/etc/passwd",
            "linked",
            "hard-a",
            "hard-b",
            "fifo",
        ):
            result, _ = self.call(name, "read_file", path)
            require(
                "synthetic-outside-unread" not in self.text(result)
                and (
                    "cannot" in self.text(result).lower()
                    or "refus" in self.text(result).lower()
                    or "accepted" in self.text(result).lower()
                    or "inaccessible" in self.text(result).lower()
                ),
                "oracle",
            )
        require(
            source_manifest(self.root / "share-input-a") == before, "source-mutated"
        )
        self.check("traversal_link_special_refusals")
        if self.args.mount_probe:
            target = self.root / "share-input-a/nested-mount"
            with NestedMount(self.root / "mount-input", target) as mounted:
                self.active_mount = mounted
                try:
                    result, _ = self.call(name, "read_file", "nested-mount/mounted.txt")
                    require(
                        "crosses a mount" in self.text(result)
                        and "MOUNTED_NOT_EXPOSED" not in self.text(result),
                        "mount-probe",
                    )
                finally:
                    self.active_mount = None
            self.verify_inputs()
            self.check("nested_mount_refusal")
        denied, _ = self.call(
            name,
            "read_file",
            "text.txt",
            permissions=[
                {"action": "*", "resource": "*", "effect": "allow"},
                {"action": name + "_read_file", "resource": "*", "effect": "deny"},
            ],
        )
        require(
            "synthetic-live-after" not in self.text(denied)
            and (
                "den" in self.text(denied).lower()
                or "Unknown tool" in self.text(denied)
            ),
            "oracle",
        )
        denied_id = self.last_session
        evaluated = self.request(
            "POST",
            "/api/session/" + denied_id + "/permission",
            {"action": name + "_read_file", "resources": ["*"], "agent": "build"},
        )["data"]
        require(evaluated.get("effect") == "deny", "oracle")
        require(
            self.request("GET", "/api/session/" + denied_id + "/permission")["data"]
            == [],
            "pending-permission",
        )
        self.check("native_denial")
        self.read_unchanged(name, "synthetic-live-after 🙂")
        outside = str(self.root / "share-input-a/text.txt")
        result, _ = self.invoke(
            "shell",
            {
                "command": "python3 -c "
                + __import__("shlex").quote(
                    "import pathlib; assert not pathlib.Path("
                    + repr(outside)
                    + ").exists(); print('OUTSIDE_PATH_UNAVAILABLE')"
                )
            },
        )
        require("OUTSIDE_PATH_UNAVAILABLE" in self.text(result))
        self.check("ordinary_shell_isolation")
        result, _ = self.invoke("read", {"path": outside})
        require(
            "synthetic-live-after" not in self.text(result) and self.text(result),
            "oracle",
        )
        self.check("ordinary_read_isolation")
        image, image_name = self.accepted_share(single=True, ack=True)
        result, messages = self.call(image_name, "read_image")
        require(
            "data:image/png;base64," + PNG in result.get("images", [])
            and any(
                node.get("type") == "file" and node.get("mime") == "image/png"
                for node in nodes(messages)
            ),
            "oracle",
        )
        self.check("single_file_image")
        self.read_unchanged(name, "synthetic-live-after 🙂")
        self.check("two_shares")
        image.send(b"\x03")
        image.finish()
        self.unknown(image_name)
        self.check("ctrl_c_revocation")
        other_owner = self.owner("source-b")
        other_share, other_name = self.accepted_share(
            "source-b", "share-input-b", ack=True
        )
        self.read_unchanged(
            other_name,
            "synthetic-live-before 中",
            project="source-b",
            input_name="share-input-b",
        )
        self.read_unchanged(name, "synthetic-live-after 🙂")
        self.check("two_workspaces")
        self.unknown(name, project="source-b")
        self.unknown(other_name, project="source-a")
        self.check("cross_workspace_refusal")
        os.kill(other_share.process.pid, signal.SIGKILL)
        other_share.finish(-signal.SIGKILL)
        wait(
            lambda: self.service.controller_running(
                self.box, self.f.workspace(self.root, "source-b")[1]
            ),
            timeout=10,
        )
        self.unknown(other_name, project="source-b")
        self.check("killed_client_revocation")
        other_owner.finish()
        self.request("POST", "/api/location/reload")
        shared.finish()
        self.unknown(name)
        self.check("reload_revocation")
        unrelated, _ = self.invoke(
            "execute", {"code": "return await tools.unrelated.check({})"}
        )
        require(self.text(unrelated) == "UNRELATED_SHARING_MCP_OK")
        self.check("unrelated_mcp_preserved")
        hold, hold_name = self.accepted_share(ack=True)
        path, data = self.f.workspace(self.root, "source-a")
        checkpoint = self.box.session_state(path).get("synced")
        owner.release()
        require(owner.message(timeout=30) == {"wait_clients": 1}, "oracle")
        # All probe leases have ended. Only the production share owns a client
        # lease here; three separate real-controller observations span the wait.
        for _ in range(3):
            time.sleep(0.3)
            require(owner.process.poll() is None)
            require(
                self.service.controller_running(
                    self.box, self.f.workspace(self.root, "source-a")[1]
                )
            )
            require(self.f.verify_owned_resources(self.root, data)["State"]["Running"])
            require(self.box.session_state(path).get("synced") == checkpoint)
            try:
                with self.box.workspace_lock(path):
                    raise Refusal("oracle")
            except self.box.BoxError:
                pass
        self.check("owner_waits_only_for_share")
        hold.send(b"\x03")
        hold.finish()
        owner.finish()
        self.check("lease_release_stops_owner")
        resumed = self.owner(expect_stop=True)
        self.no_regrant("source-a")
        self.check("resume_no_regrant")
        stopped_share, stopped_name = self.accepted_share(ack=True)
        data = self.f.workspace(self.root, "source-a")[1]
        self.box.stop_container(data)
        stopped_share.finish()
        resumed.release()
        self.exits.append(resumed.finish(timeout=180))
        self.f.verify_owned_resources(self.root, data, stopped=True)
        restarted = self.owner()
        self.no_regrant("source-a")
        restarted.finish()
        self.check("explicit_stop_revocation")
        reset = self.owner(reset=True)
        self.no_regrant("source-a")
        reset.finish()
        self.check("reset_no_regrant")

    def backup(self):
        self.stage = "backup-restore"
        records = self.validate()
        path, data = records["source-a"]
        self.f.verify_owned_resources(self.root, data, stopped=True)
        before = source_manifest(self.root / "share-input-a")
        identity = self.root / "sharing-age-identity.txt"
        descriptor = os.open(identity, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(self.f.crypto.small(["age-keygen"]))
        recipient = (
            self.f.crypto.small(["age-keygen", "-y", str(identity)]).decode().strip()
        )
        self.f.crypto.validate_recipients([recipient])
        archive = self.root / "sharing-full.ocbox"
        require(
            self.box.main(
                [
                    "backup",
                    "--workspace",
                    data["id"],
                    "--output",
                    str(archive),
                    "--recipient",
                    recipient,
                ]
            )
            == 0,
            "backup",
        )
        # Never stage plaintext. Inspect all tar contents and OCI layers, not
        # compressed ciphertext bytes. Disclosed tool results/history may remain.
        scanner = Path(__file__).resolve().with_name("sharing_archive_scan.py")
        with self.f.crypto.opened(archive, [identity]) as (
            manifest,
            directory,
            private,
        ):
            self.f.backups.validate_manifest(self.box, manifest)
            require(
                manifest["kind"] == "full"
                and set(manifest["components"])
                == {"project.age", "home.age", "registry.age", "image.age"},
                "backup",
            )
            for name in ("project.age", "home.age", "registry.age", "image.age"):
                self.f.crypto.decrypt_stream(
                    directory / name,
                    private,
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        str(scanner),
                        "oci" if name == "image.age" else "tar",
                    ],
                )
        self.check("backup_external_tree_excluded")
        ciphertext = digest(archive)
        destination = self.root / "restored-full"
        require(not destination.exists(), "backup")
        require(
            self.box.main(
                [
                    "restore",
                    str(archive),
                    "--project",
                    str(destination),
                    "--identity",
                    str(identity),
                ]
            )
            == 0,
            "backup",
        )
        self.f.record_ids(self.root)
        restored_path, restored = self.f.workspace(self.root, "restored-full")
        self.f.verify_owned_resources(self.root, restored, stopped=True)
        require(
            restored["id"] != data["id"]
            and self.box.container_project(restored)
            == self.box.container_project(data),
            "backup",
        )
        self.check("restore_fresh_stopped_identity")
        owner = self.owner("restored-full")
        self.no_regrant("restored-full")
        invisible = self.container_python(
            "import json,pathlib,sys; print(json.dumps(not pathlib.Path(json.load(sys.stdin)).exists()))",
            str(self.root / "share-input-a"),
            project="restored-full",
        )
        require(invisible is True)
        owner.finish()
        self.check("restore_no_regrant")
        require(
            source_manifest(self.root / "share-input-a") == before
            and digest(archive) == ciphertext,
            "source-mutated",
        )
        self.check("source_and_ciphertext_preserved")

    def journal(self):
        validate_journal(self.root / "registry/log")
        self.check("journal_private_bounded_no_contents")

    def run(self):
        self.core()
        self.backup()
        self.journal()
        self.verify_inputs()
        records = self.validate()
        for _, data in records.values():
            self.f.verify_owned_resources(self.root, data, stopped=True)
        self.check("owners_stopped")
        self.same_source()
        self.check("unchanged_source_and_harness")
        self.f.assert_host_clean()
        self.check("host_clean")
        self.stage = "complete"

    def close(self):
        for child in reversed([*self.owners, *self.shares]):
            try:
                child.close()
            except BaseException as error:
                self.secondary.append(category(error))
            finally:
                if getattr(child, "forced", False) is True:
                    self.secondary.append("forced-owner-termination")
            try:
                if isinstance(child, PipeChild) and child.require_terminal:
                    child.validate_terminal()
            except BaseException as error:
                self.secondary.append(category(error))
        if self.expected_inputs:
            try:
                self.verify_inputs()
            except BaseException as error:
                self.secondary.append(category(error))


def report_value(harness, loaded, runner, primary, failure=None):
    require(valid_trace([] if failure is None else failure), "protocol")
    checks = {} if harness is None else harness.checks
    secondary = [] if harness is None else harness.secondary
    unavailable = (
        list(EXTRA_CHECKS)
        if harness is None
        else sorted(set(harness.unavailable) | (set(EXTRA_CHECKS) - checks.keys()))
    )
    if primary is None and (
        not set(CHECKS) <= checks.keys()
        or not checks.keys() <= set((*CHECKS, *EXTRA_CHECKS))
        or not all(value is True for value in checks.values())
        or (
            harness is not None
            and (harness.stage != "complete" or harness.provider.errors != 0)
        )
        or not set(unavailable) <= set(EXTRA_CHECKS)
        or bool(set(unavailable) & checks.keys())
    ):
        primary = "missing-checks"
    return {
        "format": FORMAT,
        "status": "FAIL"
        if primary or secondary
        else "INCOMPLETE"
        if unavailable
        else "PASS",
        "stage": "preparation" if harness is None else harness.stage,
        "source_identity": loaded.identity,
        "harness_sha256": runner,
        "native": None if harness is None else harness.native,
        "intentional_stop_owner_exits": []
        if harness is None
        else getattr(harness, "exits", []),
        "checks": checks,
        "not_run": unavailable,
        "primary_category": primary,
        "secondary_categories": secondary,
        "failure_trace": [] if failure is None else failure,
        "owner_diagnostics": []
        if harness is None
        else [
            child.diagnostic()
            for child in getattr(harness, "owners", [])
            if isinstance(child, PipeChild)
        ],
        "provider": None
        if harness is None
        else {"requests": harness.provider.requests, "errors": harness.provider.errors},
        "provider_diagnostics": None
        if harness is None
        or not callable(getattr(harness.provider, "diagnostics", None))
        else harness.provider.diagnostics(),
        "scope": "Synthetic external-host production sharing CLI/PTY, real rootless workspace/native tools; no real providers/projects or security certification.",
        "manual_observations": "NOT_AUTOMATICALLY_PASSED",
    }


def main(argv=None):
    args = parser().parse_args(argv)
    root = loaded = harness = provider = None
    primary = None
    control = None
    failure = []
    output = os.fdopen(os.dup(1), "w", buffering=1)
    try:
        # No fixture/source/private engine read before the external host fence.
        history, provider_path = helpers()
        history.host_guard()
        validate_options(args)
        if args.child == "owner":
            control = owner_control()
            owner_stage(control, "owner-source")
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        history, loaded, provider_module = load(args)
        require(args.distro in loaded.box.DISTROS, "invalid-options")
        loaded.history_identity = lambda: history.source_snapshot(
            loaded.source, args.expect_commit
        )
        if args.child:
            return child(args, loaded, control=control)
        parent = history.fresh_parent(args.work_dir)
        temporary = history.fresh_parent(args.temp_dir) if args.temp_dir else None
        require(
            not parent.is_relative_to(loaded.source)
            and (temporary is None or not temporary.is_relative_to(loaded.source)),
            "placement",
        )
        binary = loaded.interactive.native_binary(args.opencode_v2)
        require(not binary.is_relative_to(loaded.source), "placement")
        harness_root = Path(__file__).resolve().parents[1]
        require(
            not parent.is_relative_to(harness_root)
            and (temporary is None or not temporary.is_relative_to(harness_root))
            and not binary.is_relative_to(harness_root),
            "placement",
        )
        native_podman = shutil.which(
            "podman", path=loaded.box.maintenance_host_env().get("PATH", os.defpath)
        )
        require(native_podman is not None, "private-store")
        require(
            callable(getattr(os, "memfd_create", None))
            and shutil.which("age") is not None
            and shutil.which("age-keygen") is not None,
            "missing-host-scope",
        )
        enforcing = (
            Path("/sys/fs/selinux/enforce").exists()
            and Path("/sys/fs/selinux/enforce").read_text().strip() == "1"
        )
        require(not args.require_selinux or enforcing, "missing-host-scope")
        if args.mount_probe:
            require(
                shutil.which("bindfs") is not None
                and shutil.which("fusermount3") is not None,
                "mount-probe",
            )
        f = loaded.fixture
        root = Path(
            __import__("tempfile").mkdtemp(prefix=f.PREFIX + "sharing-", dir=parent)
        )
        root.chmod(0o700)
        print("Retained sharing root: " + str(root), file=output)
        runner = harness_hashes()
        provider = provider_module.Provider()
        harness = Harness(args, history, loaded, provider, root)
        harness.output = output
        if not enforcing:
            harness.unavailable.append("enforcing_selinux_source_labels_unchanged")
        if not args.mount_probe:
            harness.unavailable.append("nested_mount_refusal")
        with f.private_output():
            f.prepare_root(root, args.distro)
            f.prepare_podman_cli(root, Path(native_podman).resolve(strict=True))
            config = provider.config(
                unrelated_command=["python3", "-u", "-c", UNRELATED_MCP]
            )
            f.write_json_new(root / "config/opencode/opencode.json", config)
            with (
                patch.dict(os.environ, fixture_environment(f, root), clear=True),
                f.temporary_location(root, temporary),
            ):
                f.validate_store(root)
                f.prepare_native_cli(root, binary)
                f.write_json_new(
                    root / MARKER, purpose(root, loaded, runner, binary, config)
                )
                for name in INPUTS:
                    directory = root / name
                    directory.mkdir(mode=0o700)
                    (directory / "text.txt").write_text("synthetic-live-before 中")
                    (directory / ".hidden").write_text("synthetic-hidden")
                    (directory / "pixel.png").write_bytes(base64.b64decode(PNG))
                    (directory / "UNREAD_EXTERNAL_TREE_SENTINEL").write_text(
                        "SHARING_NEVER_READ_CONTENT_" + os.urandom(24).hex()
                    )
                    (directory / "linked").symlink_to(root / "outside-not-shared.txt")
                    (directory / "hard-a").write_text("synthetic-hardlink")
                    os.link(directory / "hard-a", directory / "hard-b")
                    os.mkfifo(directory / "fifo", 0o600)
                (root / "share-input-a/nested-mount").mkdir(mode=0o700)
                (root / "mount-input").mkdir(mode=0o700)
                (root / "mount-input/mounted.txt").write_text("MOUNTED_NOT_EXPOSED")
                (root / "outside-not-shared.txt").write_text("synthetic-outside-unread")
                labels = {name: source_manifest(root / name) for name in INPUTS}
                harness.capture_inputs()
                harness.run()
                if enforcing:
                    for name in INPUTS:
                        current = source_manifest(root / name)
                        require(
                            all(
                                current[path][7] == info[7]
                                for path, info in labels[name].items()
                            ),
                            "source-mutated",
                        )
                        require(current["."][7] is not None, "missing-host-scope")
                    for _, data in f.workspace_records(root).values():
                        info = f.verify_owned_resources(root, data, stopped=True)
                        require(
                            bool(info.get("ProcessLabel"))
                            and not any(
                                "label=disable" in item
                                for item in info.get("HostConfig", {}).get(
                                    "SecurityOpt", []
                                )
                            ),
                            "missing-host-scope",
                        )
                    harness.check("enforcing_selinux_source_labels_unchanged")
    except BaseException as error:
        primary = category(error)
        failure = failure_trace(error)
        if control is not None:
            try:
                print(
                    json.dumps({"error": primary, "failure_trace": failure}),
                    file=control,
                    flush=True,
                )
            except (OSError, ValueError):
                pass  # The failed parent/pipe is not permission to log private output.
    finally:
        if harness is not None:
            harness.close()
        if provider is not None:
            try:
                provider.close()
            except BaseException as error:
                if harness is not None:
                    harness.secondary.append(category(error))
        if not args.child and root is not None and loaded is not None:
            try:
                value = report_value(harness, loaded, runner, primary, failure)
                loaded.fixture.write_json_new(root / REPORT, value)
                print("Report: " + str(root / REPORT), file=output)
                print("sharing fixture: " + value["status"], file=output)
                primary = primary or (
                    "missing-host-scope" if value["status"] != "PASS" else None
                )
            except BaseException:
                primary = primary or "operation-failed"
        if primary:
            print("sharing fixture: category=" + primary, file=output)
        if control is not None:
            control.close()
        output.close()
    return 1 if primary else 0


if __name__ == "__main__":
    sys.exit(main())
