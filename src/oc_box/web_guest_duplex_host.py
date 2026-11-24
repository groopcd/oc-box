"""Unserved host supervision of one already registered guest duplex exec."""

from collections import deque
from dataclasses import dataclass
import io
import os
import secrets
import select
import signal
import stat
import subprocess
from threading import current_thread
from time import monotonic

if __package__:
    from . import web_guest_duplex_worker as wire
else:
    import web_guest_duplex_worker as wire


class DuplexHostError(ValueError):
    def __init__(self):
        super().__init__("Studio guest tunnel retirement could not be verified.")


def _pin(pipe):
    if type(pipe) is not io.FileIO or pipe.closed:
        raise DuplexHostError()
    return _fd_pin(pipe.fileno())


def _fd_pin(fd):
    info = os.fstat(fd)
    if not stat.S_ISFIFO(info.st_mode) or os.get_inheritable(fd):
        raise DuplexHostError()
    return fd, info.st_dev, info.st_ino


@dataclass(slots=True, repr=False)
class _Frame:
    op: wire.Op
    value: bytes
    sent: int = 0


class DuplexHost:
    """Exclusive, single-threaded use of the original unbuffered exec pipes.

    The future launcher owns pre-spawn registration, immutable container
    selection and cancellation delivery. It must hand over its original
    unreaped Popen, never a looked-up PID, and never touch the streams or
    poll/wait that process after handoff. This class does not spawn an exec,
    own a browser socket, or release a workspace.
    """

    def __init__(self, child):
        if (
            type(child) is not subprocess.Popen
            or child.returncode is not None
            or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL
        ):
            raise DuplexHostError()
        self._owner = current_thread()
        self._child = child
        self._process = os.getpid()
        self._pid = child.pid
        tx, rx = _pin(child.stdin), _pin(child.stdout)
        if tx[1:] == rx[1:]:
            raise DuplexHostError()
        self._tag = secrets.token_bytes(wire.TAG_SIZE)
        self._out = deque()
        self._input = bytearray()
        self._header = None
        self._open_queued = self._opened = self.ready = self.ended = False
        self._queued_data = self._rx_data = self._rx_credit = None
        self._send_serial = self._receive_serial = 0
        self._closing = self._cancel_sent = self._input_closed = False
        self._receipt = self._eof = self._failed = self._verified = False
        # Raw descriptors have no FileIO finalizer which could close a later
        # foreign descriptor after observed numeric-FD substitution.
        owned = []
        try:
            owned.append(os.pidfd_open(self._pid))
            owned.append(os.dup(tx[0]))
            owned.append(os.dup(rx[0]))
            self._pidfd, tx_fd, rx_fd = owned
            self._pid_pin = self._pidfd_identity()
            self._tx_pin, self._rx_pin = _fd_pin(tx_fd), _fd_pin(rx_fd)
            os.set_blocking(self._tx_pin[0], False)
            os.set_blocking(self._rx_pin[0], False)
            if (
                _pin(child.stdin) != tx
                or _pin(child.stdout) != rx
                or self._tx_pin[1:] != tx[1:]
                or self._rx_pin[1:] != rx[1:]
            ):
                raise DuplexHostError()
            child.stdin.close()
            child.stdout.close()
        except BaseException:
            for fd in reversed(owned):
                os.close(fd)
            raise
        self._output_closed = False

    def _thread(self):
        if os.getpid() != self._process or current_thread() is not self._owner:
            raise DuplexHostError()

    def _pidfd_identity(self):
        row = os.fstat(self._pidfd)
        return row.st_dev, row.st_ino

    def _usable(self):
        self._thread()
        if self._failed or self._closing or self._verified:
            raise DuplexHostError()

    def open(self):
        self._usable()
        if self._open_queued:
            raise DuplexHostError()
        self._open_queued = True
        self._append(wire.Op.OPEN, self._tag)

    def _append(self, op, payload):
        if len(self._out) >= 2:
            raise DuplexHostError()
        self._out.append(_Frame(op, wire.encode(op, payload)))

    def send(self, data):
        """Queue one bounded frame, never reuse unreturned native credit."""
        self._usable()
        if type(data) is not bytes or not 0 < len(data) <= wire.MAX_DATA:
            raise DuplexHostError()
        if not self.ready or self.ended or self._queued_data is not None:
            return False
        serial = self._send_serial + 1
        if serial >= 2**64:
            raise DuplexHostError()
        self._append(wire.Op.DATA, serial.to_bytes(8, "big") + data)
        self._queued_data = serial
        return True

    def receive(self):
        """Borrow the pending bytes until the caller can actually consume them."""
        self._usable()
        return self._rx_data

    def consumed(self):
        """Return native credit only after the bounded sink took the frame."""
        self._usable()
        if self._rx_data is None:
            raise DuplexHostError()
        self._append(wire.Op.ACK, self._receive_serial.to_bytes(8, "big"))
        self._rx_data = None

    def _check_pipes(self):
        if (
            _fd_pin(self._rx_pin[0]) != self._rx_pin
            or self._child.returncode is not None
            or (not self._input_closed and _fd_pin(self._tx_pin[0]) != self._tx_pin)
        ):
            raise DuplexHostError()

    def _write(self):
        row = self._out[0]
        try:
            count = os.write(self._tx_pin[0], memoryview(row.value)[row.sent :])
        except BlockingIOError:
            return
        if count <= 0:
            raise DuplexHostError()
        row.sent += count
        if row.sent != len(row.value):
            return
        self._out.popleft()
        if row.op == wire.Op.OPEN:
            self._opened = True
        elif row.op == wire.Op.ACK:
            self._rx_credit = None
        elif row.op == wire.Op.DATA:
            self._send_serial += 1
        elif row.op == wire.Op.CANCEL:
            if _fd_pin(self._tx_pin[0]) != self._tx_pin or self._out:
                raise DuplexHostError()
            self._cancel_sent = True
            os.close(self._tx_pin[0])
            self._input_closed = True

    def _read(self):
        if self._receipt:
            limit = 1  # The original pipe must give EOF, not even one surplus byte.
        else:
            size = wire._HEADER.size if self._header is None else self._header[1]
            limit = size - len(self._input)
        try:
            data = os.read(self._rx_pin[0], limit)
        except BlockingIOError:
            return
        if not data:
            if not self._receipt:
                raise DuplexHostError()
            self._eof = True
            return
        if self._receipt:
            raise DuplexHostError()
        self._input.extend(data)
        if len(self._input) != size:
            return
        if self._header is None:
            self._header = wire.decode_header(self._input)
            self._input.clear()
            if self._header[1] != 0:
                return
        op = self._header[0]
        payload = bytes(self._input)
        self._input.clear()
        self._header = None
        self._accept(op, payload)

    def _accept(self, op, payload):
        if op == wire.Op.CLOSED and self._cancel_sent and self._input_closed:
            expected = (
                wire.Closure.SOCKET_CLOSED
                if self._opened
                else wire.Closure.NEVER_OPENED
            )
            if payload != self._tag + bytes([expected]):
                raise DuplexHostError()
            self._receipt = True
        elif op == wire.Op.READY and self._opened and not self.ready:
            if payload != self._tag:
                raise DuplexHostError()
            self.ready = True
        elif op == wire.Op.DATA and self.ready and not self.ended:
            serial = int.from_bytes(payload[:8], "big")
            if self._rx_credit is not None or serial != self._receive_serial + 1:
                raise DuplexHostError()
            self._receive_serial = self._rx_credit = serial
            if not self._closing:
                self._rx_data = payload[8:]
        elif op == wire.Op.ACK and self.ready and not self.ended:
            serial = int.from_bytes(payload, "big")
            if not serial or serial != self._queued_data or serial != self._send_serial:
                raise DuplexHostError()
            self._queued_data = None
        elif (
            op == wire.Op.END
            and self.ready
            and not self.ended
            and self._rx_credit is None
        ):
            self.ended = True
        else:
            raise DuplexHostError()

    def _step(self, milliseconds):
        self._check_pipes()
        poller = select.poll()
        if self._out:
            poller.register(self._tx_pin[0], select.POLLOUT)
        if not self._eof:
            poller.register(self._rx_pin[0], select.POLLIN)
        events = dict(poller.poll(milliseconds))
        self._check_pipes()
        # Accept only bounded records. Output ownership is never handed to a
        # browser writer; the caller owns its own independent sink bounds.
        for event in events.values():
            if event & select.POLLNVAL:
                raise DuplexHostError()
        if self._tx_pin[0] in events and self._out:
            self._write()
        if self._rx_pin[0] in events and not self._eof:
            self._read()

    def advance(self, *, milliseconds=0):
        """One short poll slice for a future duplex pump, not a lifetime timer."""
        self._usable()
        if type(milliseconds) is not int or not 0 <= milliseconds <= 100:
            raise DuplexHostError()
        try:
            self._step(milliseconds)
        except (OSError, ValueError):
            self._failed = True
            raise DuplexHostError() from None
        except BaseException:
            self._failed = True
            raise

    def _wait_exact(self, deadline):
        child = self._child
        if self._pidfd_identity() != self._pid_pin:
            raise DuplexHostError()
        poller = select.poll()
        poller.register(self._pidfd, select.POLLIN)
        while monotonic() < deadline:
            if (
                child.pid != self._pid
                or child.returncode is not None
                or self._pidfd_identity() != self._pid_pin
            ):
                raise DuplexHostError()
            pid, status = os.waitpid(self._pid, os.WNOHANG)
            if pid == self._pid and (os.WIFEXITED(status) or os.WIFSIGNALED(status)):
                child.returncode = os.waitstatus_to_exitcode(status)
                return child.returncode
            if pid:
                raise DuplexHostError()
            poller.poll(_slice(deadline))
        raise DuplexHostError()

    def close(self, *, timeout=5.0):
        """Spend one cancellation; require exact CLOSED, real EOF and clean reap."""
        self._thread()
        if type(timeout) not in (int, float) or not 0 < timeout <= 5:
            raise DuplexHostError()
        if self._verified:
            return True
        if self._closing or self._failed:
            return False
        self._closing = True
        deadline = monotonic() + timeout
        try:
            # Preserve only the already-written prefix's framing. No other
            # queued data is allowed to cross the cancellation boundary.
            partial = self._out[0] if self._out and self._out[0].sent else None
            self._out.clear()
            self._rx_data = None
            if partial is not None:
                self._out.append(partial)
            self._append(wire.Op.CANCEL, self._tag)
            while not self._eof and monotonic() < deadline:
                self._step(_slice(deadline))
            if (
                not (self._receipt and self._eof)
                or self._wait_exact(deadline) != 0
                or monotonic() >= deadline
                or _fd_pin(self._rx_pin[0]) != self._rx_pin
                or self._pidfd_identity() != self._pid_pin
            ):
                return False
            os.close(self._rx_pin[0])
            self._output_closed = True
            os.close(self._pidfd)
            self._pidfd = None
            self._verified = True
            return True
        except (OSError, ValueError):
            return False
        finally:
            if not self._verified:
                self._failed = True

    def abandon(self, *, timeout=1.0):
        """Best-effort local cleanup only, never confirmation or a second CANCEL."""
        self._thread()
        if type(timeout) not in (int, float) or not 0 < timeout <= 1:
            raise DuplexHostError()
        if self._verified:
            return
        self._failed = True
        for flag, expected in (
            ("_input_closed", self._tx_pin),
            ("_output_closed", self._rx_pin),
        ):
            if getattr(self, flag):
                continue
            setattr(self, flag, True)
            try:
                # Raw ownership leaves no finalizer on a substituted FD.
                if _fd_pin(expected[0]) == expected:
                    os.close(expected[0])
            except (OSError, ValueError):
                pass
        if self._pidfd is None:
            return
        reaped = self._child.returncode is not None
        try:
            if (
                self._pidfd_identity() == self._pid_pin
                and self._child.pid == self._pid
                and self._child.returncode is None
            ):
                try:
                    signal.pidfd_send_signal(self._pidfd, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self._wait_exact(monotonic() + timeout)
                reaped = True
        except ChildProcessError:
            # This process has no remaining child to reap by this PID.
            # That is not a terminal wait status or a clean receipt.
            reaped = True
        except (OSError, ValueError):
            pass
        finally:
            try:
                if self._pidfd_identity() != self._pid_pin:
                    self._pidfd = None
                elif reaped:
                    os.close(self._pidfd)
                    self._pidfd = None
            except OSError:
                self._pidfd = None


def _slice(deadline):
    return min(50, max(0, int((deadline - monotonic()) * 1000)))
