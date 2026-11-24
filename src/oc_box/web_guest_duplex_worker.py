"""Unserved native-side duplex proof over the original exec's private pipes."""

from collections import deque
from dataclasses import dataclass
from enum import IntEnum
import errno
import math
import os
import select
import socket
import stat
import struct
import sys
from time import monotonic


MAX_DATA = 16 * 1024
TAG_SIZE = 16
_VERSION = b"OGD\x01"
_HEADER = struct.Struct("!4sBH")
_SERIAL = struct.Struct("!Q")
_START_SECONDS = 6
_FRAME_SECONDS = 15
_CLOSE_SECONDS = 5


class Op(IntEnum):
    OPEN = 1
    READY = 2
    DATA = 3
    ACK = 4
    END = 5
    CANCEL = 6
    CLOSED = 7


class Closure(IntEnum):
    NEVER_OPENED = 0
    SOCKET_CLOSED = 1


class DuplexError(ValueError):
    def __init__(self):
        super().__init__("Native duplex worker protocol failed.")


def _check_size(op, size):
    if op == Op.DATA:
        valid = _SERIAL.size < size <= _SERIAL.size + MAX_DATA
    elif op == Op.ACK:
        valid = size == _SERIAL.size
    elif op == Op.END:
        valid = size == 0
    elif op == Op.CLOSED:
        valid = size == TAG_SIZE + 1
    else:
        valid = size == TAG_SIZE
    if not valid:
        raise DuplexError()


def encode(op: Op, payload: bytes) -> bytes:
    if type(op) is not Op or type(payload) is not bytes:
        raise DuplexError()
    _check_size(op, len(payload))
    return _HEADER.pack(_VERSION, op, len(payload)) + payload


def decode_header(header: bytes | bytearray) -> tuple[Op, int]:
    if len(header) != _HEADER.size:
        raise DuplexError()
    version, code, size = _HEADER.unpack(header)
    try:
        op = Op(code)
    except ValueError:
        raise DuplexError() from None
    if version != _VERSION:
        raise DuplexError()
    _check_size(op, size)
    return op, size


@dataclass
class _Output:
    op: Op
    wire: bytes
    deadline: float
    sent: int = 0


class _Worker:
    def __init__(self, port):
        self.port = port
        self.native = None
        self.opened = self.connecting = self.ready = self.ended = False
        self.tag = None
        self.cancelled = self.input_eof = self.done = False
        self.phase_deadline = monotonic() + _START_SECONDS
        self.partial_deadline = self.write_deadline = self.credit_deadline = None
        self.input = bytearray()
        self.header = None
        self.output = deque()
        self.to_native = None
        self.input_serial = self.output_serial = 0
        self.owe_ack = self.await_ack = None
        self.data_emitted = False

    def _send(self, op, payload=b""):
        # At most one DATA and one ACK, or one terminal record behind a
        # partially emitted frame. READY/END do not create further credit.
        if len(self.output) >= 2:
            raise DuplexError()
        deadline = (
            self.phase_deadline if self.cancelled else monotonic() + _FRAME_SECONDS
        )
        self.output.append(_Output(op, encode(op, payload), deadline))

    def _close_native(self):
        if self.native is not None:
            native, self.native = self.native, None
            native.close()  # No duplicate, linger setting, or reopen.

    def _connected(self):
        self.connecting = False
        self._send(Op.READY, self.tag)

    def _open(self, tag):
        self.tag = tag
        self.native = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.opened = True
        self.native.setblocking(False)
        status = self.native.connect_ex(("127.0.0.1", self.port))
        if status == 0:
            self._connected()
        elif status in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EINTR):
            self.connecting = True
        else:
            raise DuplexError()

    def _cancel(self, tag):
        if self.tag is not None and tag != self.tag:
            raise DuplexError()
        self.tag = tag
        self.cancelled = True
        self.phase_deadline = monotonic() + _CLOSE_SECONDS
        self._close_native()
        self.to_native = None
        # A frame with an emitted prefix must finish before CLOSED can be
        # decoded. Discard every record that has not started; no new DATA.
        partial = self.output[0] if self.output and self.output[0].sent else None
        self.output.clear()
        if partial is not None:
            self.output.append(partial)

    def _receive(self, op, payload):
        if op == Op.CANCEL:
            self._cancel(payload)
        elif self.tag is None and op == Op.OPEN:
            self._open(payload)
        elif self.ready and op == Op.DATA:
            serial = _SERIAL.unpack_from(payload)[0]
            if self.owe_ack is not None or serial != self.input_serial + 1:
                raise DuplexError()
            self.input_serial = self.owe_ack = serial
            # At most one DATA can cross an observed native EOF before its
            # END reaches the host. Do not send or credit that DATA.
            if not self.ended:
                self.to_native = memoryview(payload)[_SERIAL.size :]
                self.write_deadline = monotonic() + _FRAME_SECONDS
        elif self.ready and op == Op.ACK:
            if not self.data_emitted or _SERIAL.unpack(payload)[0] != self.await_ack:
                raise DuplexError()
            self.await_ack = self.credit_deadline = None
            self.data_emitted = False
        else:
            raise DuplexError()

    def _read_input(self):
        # Keep watching input even with a full native write slot or blocked
        # stdout. Read only the current bounded frame (or one trailing byte).
        try:
            if self.cancelled:
                if os.read(0, 1):
                    raise DuplexError()
                self.input_eof = True
                status = Closure.SOCKET_CLOSED if self.opened else Closure.NEVER_OPENED
                self._send(Op.CLOSED, self.tag + bytes([status]))
                return
            limit = self.header[1] if self.header is not None else _HEADER.size
            chunk = os.read(0, limit - len(self.input))
        except BlockingIOError:
            return
        if not chunk:
            raise DuplexError()  # Even record-aligned EOF is not CANCEL.
        if self.partial_deadline is None:
            self.partial_deadline = monotonic() + _FRAME_SECONDS
        self.input.extend(chunk)
        if len(self.input) < limit:
            return
        if self.header is None:
            self.header = decode_header(self.input)
            self.input.clear()
            if self.header[1]:
                return
        op = self.header[0]
        payload = bytes(self.input)
        self.input.clear()
        self.header = self.partial_deadline = None
        self._receive(op, payload)

    def _write_output(self):
        record = self.output[0]
        try:
            count = os.write(1, memoryview(record.wire)[record.sent :])
        except BlockingIOError:
            return
        if not count:
            raise DuplexError()
        record.sent += count
        if record.sent != len(record.wire):
            return
        self.output.popleft()
        if record.op == Op.CLOSED:
            self.done = True
        elif not self.cancelled:
            if record.op == Op.ACK:
                self.owe_ack = None
            elif record.op == Op.DATA:
                self.data_emitted = True
            elif record.op == Op.READY:
                self.ready = True
                self.phase_deadline = None

    def _native_events(self):
        if self.connecting:
            return select.POLLOUT
        if not self.ready:
            return 0
        read = select.POLLIN if self.await_ack is None else 0
        return read | (select.POLLOUT if self.to_native is not None else 0)

    def _pump_native(self, events):
        # Linux can report FIN and a subsequent TCP reset together. A
        # zero-byte recv in that case is not an orderly END.
        if events & (select.POLLERR | select.POLLNVAL):
            raise DuplexError()
        if self.connecting:
            if self.native.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
                raise DuplexError()
            self._connected()
            return
        if self.await_ack is None and events & (select.POLLIN | select.POLLHUP):
            try:
                data = self.native.recv(MAX_DATA)
            except BlockingIOError:
                data = None
            if data == b"":
                self._close_native()
                self.to_native = self.write_deadline = None
                self.ended = True
                self.phase_deadline = monotonic() + _FRAME_SECONDS
                self._send(Op.END)
                return
            if data:
                self.output_serial += 1
                if self.output_serial >= 2**64:
                    raise DuplexError()
                self.await_ack = self.output_serial
                self.credit_deadline = monotonic() + _FRAME_SECONDS
                self._send(Op.DATA, _SERIAL.pack(self.output_serial) + data)
        if self.to_native is not None and events & (select.POLLOUT | select.POLLHUP):
            try:
                count = self.native.send(self.to_native)
            except BlockingIOError:
                return
            if not count:
                raise DuplexError()
            self.to_native = self.to_native[count:]
            if not self.to_native:
                self.to_native = self.write_deadline = None
                self._send(Op.ACK, _SERIAL.pack(self.owe_ack))

    def _deadline(self):
        if self.cancelled:
            return self.phase_deadline
        return min(
            (
                value
                for value in (
                    self.phase_deadline,
                    self.partial_deadline,
                    self.write_deadline,
                    self.credit_deadline,
                    self.output[0].deadline if self.output else None,
                )
                if value is not None
            ),
            default=None,
        )

    def run(self):
        try:
            while not self.done:
                poller = select.poll()
                if not self.input_eof:
                    poller.register(0, select.POLLIN)
                # Observe loss of the sole stdout reader even while idle.
                # Error/hangup flags are reported regardless of the mask.
                poller.register(1, select.POLLOUT if self.output else 0)
                native = self.native
                native_fd = None
                if native is not None and (mask := self._native_events()):
                    native_fd = native.fileno()
                    poller.register(native_fd, mask)
                end = self._deadline()
                timeout = None if end is None else end - monotonic()
                if timeout is not None and timeout <= 0:
                    raise DuplexError()
                events = dict(
                    poller.poll(None if timeout is None else math.ceil(timeout * 1000))
                )
                if end is not None and monotonic() >= end:
                    raise DuplexError()
                if events.get(1, 0) & (
                    select.POLLERR | select.POLLHUP | select.POLLNVAL
                ):
                    raise DuplexError()
                if 0 in events:
                    self._read_input()
                if 1 in events and self.output:
                    self._write_output()
                if native_fd in events and self.native is native:
                    self._pump_native(events[native_fd])
        finally:
            self._close_native()


def main(args: list[str]) -> int:
    try:
        if len(args) != 1:
            raise DuplexError()
        text = args[0]
        if not 1 <= len(text) <= 5 or not text.isascii() or not text.isdecimal():
            raise DuplexError()
        port = int(text)
        if not 1 <= port <= 65535 or text != str(port):
            raise DuplexError()
        source, sink = os.fstat(0), os.fstat(1)
        if (
            not stat.S_ISFIFO(source.st_mode)
            or not stat.S_ISFIFO(sink.st_mode)
            or (source.st_dev, source.st_ino) == (sink.st_dev, sink.st_ino)
        ):
            raise DuplexError()
        os.set_blocking(0, False)
        os.set_blocking(1, False)
        _Worker(port).run()
    except (OSError, DuplexError):
        return 1  # No arguments, OS errors, or traffic in diagnostics.
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
