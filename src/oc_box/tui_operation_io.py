"""Private bounded dashboard operation channels; not a terminal/output viewer.

Passwords cross only an echo-disabled controlling PTY after explicit readiness.
Protocol frames contain allowlisted metadata, never passwords or tool output.
No Textual dependency is imported here.
"""

from dataclasses import asdict
import errno
import fcntl
import io
import json
import os
from pathlib import Path
import pty
import queue
import re
import resource
import select
import signal
import stat
import struct
import subprocess
import sys
import termios
import threading
import time

if __package__:
    from . import private_memory, secret_prompt
    from .tui_notifications import failure_notice
else:
    import private_memory
    import secret_prompt
    from tui_notifications import failure_notice

MAX_FRAME = 128 * 1024
MAX_TRAFFIC = 2 * 1024 * 1024
MAX_EVENTS = 4096
STAGES = frozenset(
    {
        "validating",
        "unlocking",
        "reviewing",
        "project",
        "home",
        "registry",
        "image",
        "encrypting",
        "publishing",
        "verifying",
        "cleanup",
    }
)
STAGE_LABELS = {
    "validating": "Validating reviewed source and paths",
    "unlocking": "Unlocking encrypted archive",
    "reviewing": "Waiting for metadata review",
    "project": "Streaming project files",
    "home": "Streaming private home and native history",
    "registry": "Streaming workspace metadata",
    "image": "Streaming container OS image",
    "encrypting": "Encrypting native session history",
    "publishing": "Publishing encrypted backup without overwrite",
    "verifying": "Verifying native history and final state",
    "cleanup": "Finishing operation cleanup",
}
_TEXT_FIELDS = frozenset(
    {
        "format",
        "kind",
        "created_at",
        "architecture",
        "source_workspace_id",
        "project",
        "container_project",
        "distro",
        "source_origin",
        "source_store",
        "source_session",
        "destination",
        "workspace_id",
        "status",
    }
)
_COUNT_FIELDS = frozenset(
    {"session_count", "message_count", "interrupted_assistant_count"}
)


class OperationIOError(RuntimeError):
    def __init__(self):
        self.reason = "request-failed"
        super().__init__(
            "Private operation channel failed; inspect the explicit target before retrying."
        )


def _fail():
    raise OperationIOError() from None


def metadata(value):
    """Validate a small explicit projection, not an arbitrary engine object."""
    if not isinstance(value, dict) or set(value) - (
        _TEXT_FIELDS | _COUNT_FIELDS | {"created_ids", "planned_ids"}
    ):
        _fail()
    if value.get("format") not in {"workspace", "session"}:
        _fail()
    for key, item in value.items():
        if key in _TEXT_FIELDS:
            if (
                not isinstance(item, str)
                or len(item.encode()) > 4096
                or not all(c.isprintable() for c in item)
            ):
                _fail()
            if key == "kind" and item not in {"full", "portable"}:
                _fail()
            if key == "status" and item not in {"stopped", "incomplete"}:
                _fail()
            if key == "source_origin" and item not in {"host", "workspace"}:
                _fail()
            if key in {"workspace_id", "source_workspace_id"} and not re.fullmatch(
                r"[0-9a-f]{32}", item
            ):
                _fail()
        elif key in _COUNT_FIELDS:
            if type(item) is not int or not 0 <= item <= 100000:
                _fail()
        else:
            recovery_ids(item)
    return value


def recovery_ids(values):
    if (
        not isinstance(values, list)
        or len(values) > 50
        or len(set(v for v in values if isinstance(v, str))) != len(values)
        or any(
            not isinstance(v, str) or not re.fullmatch(r"ses_[0-9a-f]{32}", v)
            for v in values
        )
    ):
        _fail()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _fail()
        result[key] = value
    return result


def _frame(value):
    data = json.dumps(
        value, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    ).encode("ascii")
    if not 0 < len(data) <= MAX_FRAME:
        _fail()
    return struct.pack("!I", len(data)) + data


class Frames:
    def __init__(self):
        self.pending = bytearray()
        self.total = self.count = 0

    def feed(self, chunk):
        self.total += len(chunk)
        if self.total > MAX_TRAFFIC:
            _fail()
        self.pending.extend(chunk)
        result = []
        while len(self.pending) >= 4:
            size = struct.unpack("!I", self.pending[:4])[0]
            if not 0 < size <= MAX_FRAME:
                _fail()
            if len(self.pending) < size + 4:
                break
            data = bytes(self.pending[4 : size + 4])
            del self.pending[: size + 4]
            try:
                item = json.loads(
                    data, object_pairs_hook=_pairs, parse_constant=lambda _: _fail()
                )
            except (ValueError, UnicodeError, RecursionError):
                _fail()
            self.count += 1
            if not isinstance(item, dict) or self.count > MAX_EVENTS:
                _fail()
            result.append(item)
        return result

    def end(self):
        if self.pending:
            _fail()


def _write(fd, value):
    view = memoryview(_frame(value))
    deadline = time.monotonic() + 0.25
    while view:
        try:
            count = os.write(fd, view)
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                _fail()
            continue
        except OSError:
            _fail()
        if not count:
            _fail()
        view = view[count:]


def disable_core_dumps():
    """The manager and child both join the transient secret-memory boundary."""
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_CORE)
        resource.setrlimit(resource.RLIMIT_CORE, (0, hard))
        if resource.getrlimit(resource.RLIMIT_CORE)[0] != 0:
            _fail()
    except (OSError, ValueError):
        _fail()


def _pipe(fd, direction):
    info = os.fstat(fd)
    if (
        fd < 3
        or not stat.S_ISFIFO(info.st_mode)
        or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != direction
    ):
        _fail()
    return info.st_dev, info.st_ino


class ChildInteraction:
    """Child main-thread API; a bounded watcher detects parent loss everywhere."""

    def __init__(self, events, controls, cancellation=None, on_dispatched=None):
        self.events, self.controls = events, controls
        self.sequence = self.prompt = self.review_id = 0
        self._review_counter = 0
        self._response_seen = 0
        self._active_prompt = None
        self._responses = queue.Queue(maxsize=1)
        self._stopped = threading.Event()
        self._reader = threading.Thread(target=self._watch, daemon=True)
        self.cancellation = (
            cancellation if cancellation is not None else threading.Event()
        )
        self.broken = False
        self._on_dispatched = on_dispatched

    def start(self):
        self._reader.start()
        self.emit("started")

    def close(self):
        self._stopped.set()
        self._reader.join(timeout=1)

    def _watch(self):
        frames = Frames()
        try:
            while not self._stopped.is_set():
                if not select.select([self.controls], [], [], 0.1)[0]:
                    continue
                chunk = os.read(self.controls, 65536)
                if not chunk:
                    raise OperationIOError()
                for response in frames.feed(chunk):
                    if (
                        set(response) != {"version", "id", "approved"}
                        or type(response["version"]) is not int
                        or response["version"] != 1
                        or type(response["id"]) is not int
                        or response["id"] != self.review_id
                        or response["id"] <= self._response_seen
                        or self.review_id <= 0
                        or type(response["approved"]) is not bool
                    ):
                        _fail()
                    self._response_seen = response["id"]
                    self._responses.put_nowait(response)
        except Exception:
            if not self._stopped.is_set() and not self.cancellation.is_set():
                self._stopped.set()
                os.kill(os.getpid(), signal.SIGINT)

    def emit(self, kind, **fields):
        if self.broken:
            return
        self.sequence += 1
        if self.sequence > MAX_EVENTS:
            _fail()
        try:
            _write(
                self.events,
                {"version": 1, "seq": self.sequence, "kind": kind, **fields},
            )
        except OperationIOError:
            self.broken = True
            if not self.cancellation.is_set():
                self.cancellation.set()
                raise KeyboardInterrupt from None

    def stage(self, stage):
        if stage not in STAGES:
            _fail()
        self.emit("stage", stage=stage)

    def dispatched(self):
        if self._on_dispatched is not None:
            self._on_dispatched()

    def observe_prompt(self, *, ready, purpose, maximum):
        if ready:
            if (
                self._active_prompt is not None
                or purpose not in secret_prompt.BACKUP_PURPOSES
                or maximum != 1024
            ):
                _fail()
            self.prompt += 1
            self._active_prompt = self.prompt
            self.emit("prompt", id=self.prompt, purpose=purpose, maximum=maximum)
        else:
            ident, self._active_prompt = self._active_prompt, None
            self.emit("prompt-closed", id=ident)

    def review(self, review):
        self.stage("reviewing")
        self._review_counter += 1
        self.review_id = self._review_counter
        ident = self.review_id
        self.emit("review", id=ident, metadata=metadata(review))
        while not self._stopped.is_set():
            try:
                response = self._responses.get(timeout=0.1)
            except queue.Empty:
                continue
            if response["id"] != ident:
                _fail()
            # Retire the accepted ID before any later native work begins.
            self.review_id = 0
            return response["approved"]
        raise KeyboardInterrupt

    def inspection(self, value):
        self.emit("inspection", metadata=metadata(value))

    def recovery(self, *, created_ids, attempted_ids):
        recovery_ids(created_ids)
        recovery_ids(attempted_ids)
        self.emit("recovery", created_ids=created_ids, attempted_ids=attempted_ids)


def child(runtime, descriptors, *, execute, check_request):
    """Called only after the production host guard, in a new process session."""
    request_fd, event_fd, control_fd, tty_fd = descriptors
    if (
        any(type(fd) is not int or fd < 3 for fd in descriptors)
        or len(set(descriptors)) != 4
    ):
        _fail()
    for fd in descriptors:
        os.set_inheritable(fd, False)
    identities = [
        (os.fstat(fd).st_dev, os.fstat(fd).st_ino, os.fstat(fd).st_rdev)
        for fd in descriptors
    ]
    if len(set(identities)) != len(identities):
        _fail()
    check_request(request_fd, "oc-box-dashboard-operation")
    if _pipe(event_fd, os.O_WRONLY) == _pipe(control_fd, os.O_RDONLY):
        _fail()
    if (
        not os.isatty(tty_fd)
        or not stat.S_ISCHR(os.fstat(tty_fd).st_mode)
        or os.fstat(tty_fd).st_uid != os.getuid()
        or os.fstat(tty_fd).st_mode & 0o077
    ):
        _fail()
    os.set_blocking(event_fd, False)
    fcntl.ioctl(tty_fd, termios.TIOCSCTTY, 0)
    os.tcsetpgrp(tty_fd, os.getpgrp())
    attrs = termios.tcgetattr(tty_fd)
    attrs[3] &= ~(termios.ECHO | termios.ECHONL)
    termios.tcsetattr(tty_fd, termios.TCSANOW, attrs)
    content = os.pread(request_fd, 128 * 1024 + 1, 0)
    os.close(request_fd)
    if len(content) > 128 * 1024:
        _fail()
    try:
        request = json.loads(
            content, object_pairs_hook=_pairs, parse_constant=lambda _: _fail()
        )
    except (ValueError, UnicodeError, RecursionError):
        _fail()
    disable_core_dumps()
    state = dict(
        operation="mutation",
        dispatched=False,
        completed=False,
        uncertain=False,
        cancelled=False,
    )
    cancellation = threading.Event()
    interaction = ChildInteraction(
        event_fd,
        control_fd,
        cancellation,
        on_dispatched=lambda: state.update(dispatched=True),
    )
    previous = {}
    code, error = 1, None

    def cancel(*_):
        if cancellation.is_set():
            return
        cancellation.set()
        raise KeyboardInterrupt

    try:
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[number] = signal.signal(number, cancel)
        interaction.start()
        with secret_prompt.operation_prompts(
            interaction.observe_prompt, cancellation=cancellation
        ):
            code = execute(
                runtime, request, progress=state.update, interaction=interaction
            )
        if type(code) is not int:
            code = 1
    except KeyboardInterrupt:
        cancellation.set()
        code = 130
        state["cancelled"] = True
        state["uncertain"] = state["uncertain"] or state["dispatched"]
    except Exception as caught:
        error = caught
        state["cancelled"] = getattr(caught, "reason", None) == "cancelled"
        if state["cancelled"]:
            cancellation.set()
        state["uncertain"] = state["uncertain"] or (
            state["cancelled"] and state["dispatched"]
        )
        code = 130 if state["cancelled"] else 1
    finally:
        interaction.close()
        for number, handler in previous.items():
            signal.signal(number, handler)
    # Exit zero alone is never a completed operation acknowledgement.
    if code == 0 and not state["completed"]:
        code = 1
        state["uncertain"] = state["dispatched"]
    notice = None
    if code:
        reason = "cancelled" if state["cancelled"] else None
        if (
            error is not None
            and runtime is not None
            and isinstance(error, runtime.BoxError)
            and str(error)
            == "This workspace already has an active oc-box process. Exit it before changing its environment."
        ):
            reason = "owner-active"
        notice = failure_notice(
            state["operation"],
            error,
            reason=reason,
            dispatched=state["dispatched"],
            completed=state["completed"],
            uncertain=state["uncertain"],
        )
    try:
        interaction.emit(
            "result",
            code=code,
            notice=asdict(notice) if notice else None,
            **{
                key: state[key]
                for key in ("dispatched", "completed", "uncertain", "cancelled")
            },
        )
    except (KeyboardInterrupt, Exception):
        return 1
    return code


class OperationProcess:
    """One explicit operation, one private PTY, no output history or secret cache."""

    def __init__(self, runtime, request):
        self.runtime, self.request = runtime, request
        self.process = None
        self.master = self.slave = self.events = self.controls = None
        self._lock = threading.RLock()
        self._prompt = self._review = None
        self._prompt_open = None
        self._last_prompt = self._last_review = 0
        self._ready = self._cancelled = False
        self._result = None
        self._sequence = 0
        self._outcome_admitted = False

    def start(self):
        if __package__:
            from .tui_operations import environment
        else:
            from tui_operations import environment
        value = json.dumps(self.request, ensure_ascii=True, allow_nan=False).encode()
        if len(value) > 128 * 1024 or self.process is not None:
            _fail()
        disable_core_dumps()
        event_write = control_read = None
        try:
            self.master, self.slave = pty.openpty()
            os.fchmod(self.slave, 0o600)
            attrs = termios.tcgetattr(self.slave)
            attrs[3] &= ~(termios.ECHO | termios.ECHONL)
            termios.tcsetattr(self.slave, termios.TCSANOW, attrs)
            self.events, event_write = os.pipe2(os.O_CLOEXEC)
            control_read, self.controls = os.pipe2(os.O_CLOEXEC)
            os.set_blocking(self.controls, False)
            with os.fdopen(
                private_memory.create("oc-box-dashboard-operation"), "w+b"
            ) as source:
                source.write(value)
                source.flush()
                self.process = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        str(self.runtime.PACKAGE / "tui_operations.py"),
                        "--request-fd",
                        str(source.fileno()),
                        "--event-fd",
                        str(event_write),
                        "--control-fd",
                        str(control_read),
                        "--tty-fd",
                        str(self.slave),
                    ],
                    env=environment(self.runtime),
                    cwd=Path.home(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    pass_fds=(source.fileno(), event_write, control_read, self.slave),
                )
            os.set_blocking(self.master, False)
        except BaseException:
            # No run worker exists yet. Drop every extra parent endpoint before
            # taking cancellation/drain/reap ownership ourselves; retaining the
            # event writer here would prevent the drainer from observing EOF.
            for fd in (event_write, control_read):
                if fd is not None:
                    os.close(fd)
            event_write = control_read = None
            self._abort_start()
            raise
        finally:
            for fd in (event_write, control_read):
                if fd is not None:
                    os.close(fd)

    def _abort_start(self):
        self.cancel()
        if self.controls is not None:
            os.close(self.controls)
            self.controls = None  # independent child watcher can now observe EOF
        if self.process is not None:
            try:
                self.run(lambda _event: None)
            except BaseException:
                # run() owns normal cancellation and waits for actual exit.
                # Preserve the original startup exception, never tool output.
                self.process.wait()
        self.close()

    def cancel(self):
        with self._lock:
            if self._outcome_admitted:
                return False  # verified child completion preceded this request
            self._cancelled = True
            if self._ready and self.process is not None and self.process.poll() is None:
                self._ready = False  # send once; repeated signals can interrupt cleanup
                try:
                    os.kill(self.process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
            return True

    def submit_secret(self, ident, content):
        """Synchronous bounded write only; caller releases its private editor."""
        try:
            with self._lock:
                if (
                    type(ident) is not int
                    or ident != self._prompt
                    or self._prompt is None
                    or self._cancelled
                ):
                    _fail()
                attrs = termios.tcgetattr(self.slave)
                if attrs[3] & (termios.ECHO | termios.ECHONL | termios.ICANON):
                    _fail()
                text = content.decode("utf-8")
                if len(content) > 1024 or any(
                    ord(c) < 32 or ord(c) == 127 for c in text
                ):
                    _fail()
                # Retire BEFORE delivery: the reader may accept closure plus a
                # new prompt while this thread is still returning from write.
                self._prompt = None
                content.extend(b"\n")
                if os.write(self.master, content) != len(content):
                    _fail()
        except Exception:
            self.cancel()
            raise OperationIOError() from None
        finally:
            content[:] = b"\0" * len(content)
            content.clear()

    def approve(self, ident, approved):
        with self._lock:
            if (
                ident != self._review
                or type(ident) is not int
                or self._review is None
                or type(approved) is not bool
                or self._cancelled
            ):
                _fail()
            self._review = None
            _write(self.controls, {"version": 1, "id": ident, "approved": approved})

    def _accept(self, event):
        from_notice = None
        if __package__:
            from .tui_operations import _result_notice
        else:
            from tui_operations import _result_notice
        if (
            type(event.get("version")) is not int
            or event["version"] != 1
            or type(event.get("seq")) is not int
            or event["seq"] != self._sequence + 1
            or self._result is not None
        ):
            _fail()
        self._sequence += 1
        kind = event.get("kind")
        fields = set(event) - {"version", "seq", "kind"}
        if self._sequence == 1 and kind != "started":
            _fail()
        if kind == "started":
            if fields or self._sequence != 1:
                _fail()
            self._ready = True
            if self._cancelled:
                self.cancel()
        elif kind == "stage":
            if fields != {"stage"} or event["stage"] not in STAGES:
                _fail()
        elif kind == "prompt":
            if (
                fields != {"id", "purpose", "maximum"}
                or type(event["id"]) is not int
                or event["id"] <= 0
                or event["id"] != self._last_prompt + 1
                or self._prompt is not None
                or self._prompt_open is not None
                or self._review is not None
                or event["purpose"] not in secret_prompt.BACKUP_PURPOSES
                or type(event["maximum"]) is not int
                or event["maximum"] != 1024
            ):
                _fail()
            self._prompt = event["id"]
            self._prompt_open = event["id"]
            self._last_prompt = event["id"]
        elif kind == "prompt-closed":
            if (
                fields != {"id"}
                or type(event["id"]) is not int
                or event["id"] != self._prompt_open
            ):
                _fail()
            self._prompt = None
            self._prompt_open = None
        elif kind in {"inspection", "review"}:
            if fields != ({"metadata", "id"} if kind == "review" else {"metadata"}):
                _fail()
            metadata(event["metadata"])
            if kind == "review":
                if (
                    type(event["id"]) is not int
                    or event["id"] <= 0
                    or event["id"] != self._last_review + 1
                    or self._review is not None
                    or self._prompt_open is not None
                ):
                    _fail()
                self._review = event["id"]
                self._last_review = event["id"]
        elif kind == "recovery":
            if fields != {"created_ids", "attempted_ids"}:
                _fail()
            recovery_ids(event["created_ids"])
            recovery_ids(event["attempted_ids"])
        elif kind == "result":
            if (
                fields
                != {
                    "code",
                    "notice",
                    "dispatched",
                    "completed",
                    "uncertain",
                    "cancelled",
                }
                or type(event["code"]) is not int
                or not 0 <= event["code"] <= 255
                or any(
                    type(event[k]) is not bool
                    for k in ("dispatched", "completed", "uncertain", "cancelled")
                )
            ):
                _fail()
            if event["code"] == 0 and (
                not event["completed"]
                or event["uncertain"]
                or event["cancelled"]
                or event["notice"] is not None
                or self._prompt_open is not None
                or self._review is not None
            ):
                _fail()
            if event["notice"] is not None:
                from_notice = _result_notice(
                    io.BytesIO(
                        json.dumps(
                            {"code": event["code"], "notice": event["notice"]}
                        ).encode()
                    ),
                    event["code"],
                )
                if from_notice is None:
                    _fail()
            elif (
                event["code"]
                or not event["completed"]
                or event["uncertain"]
                or event["cancelled"]
            ):
                _fail()
            self._result = event
        else:
            _fail()
        return from_notice

    def run(self, on_event):
        """Worker-thread loop. Never terminates a child just to close a screen."""
        frames = Frames()
        failed = False
        try:
            active = {self.events, self.master}
            while self.events in active:
                ready, _, _ = select.select(active, [], [], 0.1)
                for fd in ready:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError as error:
                        if fd == self.master and error.errno in (
                            errno.EIO,
                            errno.EAGAIN,
                        ):
                            if error.errno == errno.EIO:
                                active.discard(fd)
                            continue
                        raise
                    if not chunk:
                        active.discard(fd)
                        if fd == self.events:
                            frames.end()
                        continue
                    if fd == self.events and not failed:
                        try:
                            for event in frames.feed(chunk):
                                self._accept(event)
                                on_event(event)
                        except Exception:
                            failed = True
                            self.cancel()
                # PTY bytes are intentionally discarded immediately.
            code = self.process.wait()
            with self._lock:
                if (
                    failed
                    or self._result is None
                    or self._result["code"] != code
                    or (self._cancelled and code == 0)
                ):
                    _fail()
                self._outcome_admitted = True
                return self._result
        except BaseException:
            self.cancel()
            # Let the normal child finally blocks finish before releasing busy.
            if self.process is not None:
                self.process.wait()
            raise
        finally:
            self.close()

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.cancel()
            return  # the operation worker retains resources until cleanup exits
        for name in ("master", "slave", "events", "controls"):
            fd = getattr(self, name)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)
