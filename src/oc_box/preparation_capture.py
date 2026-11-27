"""Bounded, allowlisted diagnostics for quiet npm preparation commands."""

import os
import selectors
import threading
import time
from contextlib import contextmanager, suppress
from contextvars import Context
from dataclasses import dataclass


NPM_CODES = frozenset(
    {
        "EUSAGE",
        "ERESOLVE",
        "EINTEGRITY",
        "ETARGET",
        "EBADENGINE",
        "EOVERRIDE",
        "ENOTFOUND",
        "EAI_AGAIN",
        "ECONNREFUSED",
        "ECONNRESET",
        "ETIMEDOUT",
        "E401",
        "E403",
        "E404",
        "E500",
        "E502",
        "E503",
        "E504",
        "ENOENT",
        "ENOSPC",
        "EACCES",
        "EPERM",
        "ENOTCACHED",
        "ELSPROBLEMS",
        "EPIPE",
    }
)

_SOURCES = ("stdout", "stderr")
_CODE_LINES = {
    prefix + code.encode("ascii"): code
    for prefix in (b"npm ERR! code ", b"npm error code ")
    for code in NPM_CODES
}
_READ_BYTES = 16 * 1024
_MAX_LINE_BYTES = 256
_MAX_SCAN_BYTES = 1024 * 1024
_MAX_RECORDS = 64
_MAX_COUNT = 65535
_PREPARATION_FD_HEADROOM = 16
_POLL_SECONDS = 0.02
_TAIL_SECONDS = 0.1
_JOIN_SECONDS = 0.25


@dataclass
class _Capture:
    outputs: tuple[int, int]
    records: tuple[tuple[str, str, int], ...] = ()
    unavailable: bool = False


class _Records:
    def __init__(self):
        self.records = []
        self.remaining = _MAX_SCAN_BYTES
        self.limited = False
        # None means this line was already counted and is being discarded.
        self.partial = {source: bytearray() for source in _SOURCES}

    def _limit(self, source):
        if not self.limited:
            self.records.append((source, "output-limit", 1))
            self.limited = True

    def _record(self, source, code):
        if self.limited:
            return
        if self.records and self.records[-1][:2] == (source, code):
            count = self.records[-1][2]
            if count < _MAX_COUNT:
                self.records[-1] = (source, code, count + 1)
                return
        elif len(self.records) < _MAX_RECORDS - 1:
            self.records.append((source, code, 1))
            return
        self._limit(source)

    def feed(self, source, chunk):
        if self.limited:
            return
        exceeded = len(chunk) > self.remaining
        chunk = chunk[: self.remaining]
        self.remaining -= len(chunk)
        line = self.partial[source]
        start = 0
        while start < len(chunk) and not self.limited:
            end = chunk.find(b"\n", start)
            complete = end != -1
            if not complete:
                end = len(chunk)
            if line is None:
                if complete:
                    line = bytearray()
            elif len(line) + end - start > _MAX_LINE_BYTES:
                self._record(source, "omitted")
                line = bytearray() if complete else None
            else:
                line.extend(chunk[start:end])
                if complete:
                    code = _CODE_LINES.get(bytes(line).removesuffix(b"\r"), "omitted")
                    self._record(source, code)
                    line.clear()
            start = end + 1
        self.partial[source] = line
        if exceeded:
            self._limit(source)

    def finish(self, source):
        if self.partial.pop(source):
            self._record(source, "omitted")


def _close(fds):
    for fd in fds:
        with suppress(Exception):
            os.close(fd)


def check_descriptor_headroom():
    """Leave room for the original spawn, guarded admission and journal I/O."""
    probe = []
    try:
        for _ in range(_PREPARATION_FD_HEADROOM):
            probe.append(os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC))
    finally:
        _close(probe)


class _Reader:
    def __init__(self, selector, readers):
        self.selector = selector
        self.readers = readers
        self.output = _Records()
        self.scratch = (bytearray(_READ_BYTES),)
        self.deadline = None
        self.unavailable = False

    def _wait(self):
        deadline = self.deadline
        if deadline is None:
            return _POLL_SECONDS
        return min(_POLL_SECONDS, deadline - time.monotonic())

    def _collect(self):
        while self.selector.get_map():
            wait = self._wait()
            if wait <= 0:
                self.unavailable = True
                for key in self.selector.get_map().values():
                    self.output.finish(key.data)
                return
            for key, _ in self.selector.select(wait):
                try:
                    chunk = os.read(key.fd, _READ_BYTES)
                except (BlockingIOError, InterruptedError):
                    continue
                if chunk:
                    self.output.feed(key.data, chunk)
                else:
                    self.selector.unregister(key.fd)
                    self.output.finish(key.data)

    def _discard(self):
        # If capture fails, keep both pipes draining independently of the
        # selector, parser, and ordinary read path.
        pending = set(self.readers)
        while pending and (wait := self._wait()) > 0:
            progressed = False
            for fd in tuple(pending):
                try:
                    size = os.readv(fd, self.scratch)
                except (BlockingIOError, InterruptedError):
                    continue
                except Exception:
                    try:
                        size = len(os.read(fd, _READ_BYTES))
                    except Exception:
                        continue
                progressed = True
                if size == 0:
                    pending.remove(fd)
            if not progressed:
                time.sleep(wait)

    def run(self):
        try:
            try:
                self._collect()
                return
            except Exception:
                self.unavailable = True
            with suppress(Exception):
                self._discard()
        finally:
            with suppress(Exception):
                self.selector.close()
            _close(self.readers)


@contextmanager
def capture_npm():
    """Yield stdout/stderr redirections, or None on setup failure.

    Callers run their original command once inside the context. After exit,
    records follow the reader's observed order; only consecutive repetitions
    are counted together. Missing EOF at the bounded post-command deadline
    marks the capture unavailable.
    """
    readers = []
    writers = []
    selector = None
    capture = None

    def drain():
        # Thread.start() can be interrupted before it returns. Only take over
        # the reads if startup succeeded, so cleanup never races a reader.
        with starting:
            started = capture is not None
        if started:
            reader.run()

    try:
        try:
            starting = threading.Lock()
            selector = selectors.DefaultSelector()
            for source in _SOURCES:
                read_fd, write_fd = os.pipe()
                readers.append(read_fd)
                writers.append(write_fd)
                os.set_blocking(read_fd, False)
                selector.register(read_fd, selectors.EVENT_READ, source)
            # Opening the capture pipes can succeed even when they leave too
            # few descriptors for the child. Fall back before invoking it.
            check_descriptor_headroom()
            reader = _Reader(selector, readers)
            worker = threading.Thread(target=drain, daemon=True)
            with starting:
                # Python 3.14 may copy the context of the caller of start().
                Context().run(worker.start)
                capture = _Capture((writers[0], writers[1]))
        except Exception:
            pass
        finally:
            if capture is None:
                if selector is not None:
                    with suppress(Exception):
                        selector.close()
                _close(readers)
                _close(writers)
        yield capture
    finally:
        if capture is not None:
            reader.deadline = time.monotonic() + _TAIL_SECONDS
            _close(writers)
            try:
                worker.join(_JOIN_SECONDS)
                alive = worker.is_alive()
                capture.unavailable = alive or reader.unavailable
                if not alive:
                    capture.records = tuple(reader.output.records)
            except Exception:
                capture.unavailable = True
