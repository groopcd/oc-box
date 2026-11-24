"""Unwired request/offer/ack on an original counted Studio control stream."""

import math
import os
import secrets
import select
import socket
import stat
import struct
from threading import current_thread
from time import monotonic

if __package__:
    from . import web_named_stop_fence as fence
else:
    import web_named_stop_fence as fence


_MAGIC = b"OCBSTA1\n"
_REQUEST, _OFFER, _ACK = (_MAGIC + tag for tag in (b"R", b"O", b"A"))
_WIDTH = 32
_ZERO = bytes(_WIDTH)
_REQUEST_SIZE = len(_REQUEST) + 2 * _WIDTH
_OFFER_SIZE = _ACK_SIZE = len(_OFFER) + 3 * _WIDTH
_DEFAULT_TIMEOUT = 3.0
_MAX_TIMEOUT = 10.0
_BAD = select.POLLERR | select.POLLHUP | select.POLLNVAL | select.POLLRDHUP


class AttachExchangeError(ValueError):
    """Fixed local refusal. It never authorizes a fallback."""

    def __init__(self):
        super().__init__("Studio control attach could not be verified.")


def _deadline(timeout):
    if type(timeout) not in (int, float) or not 0 < timeout <= _MAX_TIMEOUT:
        raise AttachExchangeError()
    return monotonic() + timeout


def _remaining(deadline):
    left = deadline - monotonic()
    if left <= 0:
        raise AttachExchangeError()
    return left


def _descriptor(stream):
    fd = stream.fileno()
    info = os.fstat(fd)
    if not stat.S_ISSOCK(info.st_mode):
        raise AttachExchangeError()
    return fd, info.st_dev, info.st_ino


def _challenge():
    nonce = secrets.token_bytes(_WIDTH)
    if nonce == _ZERO:
        raise AttachExchangeError()
    return nonce


class _Exchange:
    """One trusted borrow, not a counted use or owner proof by construction.

    The producer owns the lifetime and creates at most one exchange per use
    on each end. It supplies the original counted use and its connected
    socket object, the *original* expected managed owner and listener, and the
    witness it captured with ``fence.observe_attach`` for that same arm. The
    socket is exclusively borrowed for these frames on the constructing
    thread. The producer must keep it held and must not alias, close, read,
    redial, reconfigure or replace it during exchange or observation. The
    borrowed socket must have Python timeout None or 0.0: the exchange uses
    its own deadline, without inheriting Python's per-operation wait.

    ``observe(use, *, deadline)`` is a trusted, read-only recheck of the
    producer's existing records. It must look up that exact still-counted
    use and return its original ``(stream, owner, listener, arm_generation)``
    only while the original managed owner and listener are still verified live
    for that arm. Otherwise it returns None. It must honor the absolute
    deadline and must not discover, repair or start anything, probe native,
    or treat peer credentials, a registration, or these frames as its proof.
    No production producer for this contract is provided here.
    """

    def __init__(self, witness, stream, *, use, owner, listener, observe):
        try:
            if (
                type(witness) is not fence._AttachWitness
                or type(witness.generation) is not bytes
                or len(witness.generation) != _WIDTH
                or witness.generation == _ZERO
                or type(stream) is not socket.socket
                or stream.getsockopt(socket.SOL_SOCKET, socket.SO_DOMAIN)
                != socket.AF_UNIX
                or stream.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
                != socket.SOCK_STREAM
                or stream.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
                or use is None
                or owner is None
                or listener is None
                or not callable(observe)
            ):
                raise AttachExchangeError()
            pinned = _descriptor(stream)
            mode = stream.gettimeout()
            if mode not in (None, 0.0):
                raise AttachExchangeError()
            stream.getpeername()  # Connected, not a path or a listen socket.
            credentials = struct.Struct("3i")
            _, uid, _ = credentials.unpack(
                stream.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, credentials.size
                )
            )
            if uid != os.getuid() or _descriptor(stream) != pinned:
                raise AttachExchangeError()
        except Exception:
            raise AttachExchangeError() from None
        self._witness, self._stream, self._stamp = witness, stream, pinned
        self._mode = mode
        self._use, self._owner, self._listener = use, owner, listener
        self._observe = observe
        self._thread = current_thread()
        self._attempted = self._complete = self._retired = False

    def _same(self):
        if (
            self._retired
            or _descriptor(self._stream) != self._stamp
            or self._stream.gettimeout() != self._mode
        ):
            raise AttachExchangeError()
        return self._stamp[0]

    def _poll(self, events):
        poll = select.poll()
        poll.register(self._same(), events | _BAD)
        return poll

    def _quiet(self):
        # Also refuse EOF and half-close; neither is a counted, held peer.
        if self._poll(select.POLLIN).poll(0):
            raise AttachExchangeError()
        self._same()

    def _ready(self, event, deadline):
        rows = self._poll(event).poll(math.ceil(_remaining(deadline) * 1000))
        mask = rows[0][1] if rows else 0
        if mask & _BAD or not (mask & event):
            raise AttachExchangeError()
        self._same()
        _remaining(deadline)

    def _read(self, size, deadline):
        frame = bytearray()
        while len(frame) < size:
            self._ready(select.POLLIN, deadline)
            try:
                piece = self._stream.recv(size + 1 - len(frame), socket.MSG_DONTWAIT)
            except BlockingIOError:
                continue
            if not piece:
                raise AttachExchangeError()
            frame.extend(piece)
        if len(frame) != size:
            raise AttachExchangeError()
        self._quiet()
        return bytes(frame)

    def _write(self, frame, deadline):
        view = memoryview(frame)
        while view:
            self._ready(select.POLLOUT, deadline)
            try:
                size = self._stream.send(
                    view, socket.MSG_DONTWAIT | socket.MSG_NOSIGNAL
                )
            except BlockingIOError:
                continue
            if size <= 0:
                raise AttachExchangeError()
            view = view[size:]

    def _live(self, deadline):
        self._same()
        if not fence.attach_current(self._witness, timeout=_remaining(deadline)):
            raise AttachExchangeError()
        _remaining(deadline)
        seen = self._observe(self._use, deadline=deadline)
        self._same()
        if (
            type(seen) is not tuple
            or len(seen) != 4
            or seen[0] is not self._stream
            or seen[1] is not self._owner
            or seen[2] is not self._listener
            or type(seen[3]) is not bytes
            or seen[3] != self._witness.generation
            or not fence.attach_current(self._witness, timeout=_remaining(deadline))
        ):
            raise AttachExchangeError()
        _remaining(deadline)
        self._same()

    def _run(self, exchange, timeout):
        if current_thread() is not self._thread or self._attempted:
            raise AttachExchangeError()
        self._attempted = True
        complete = False
        try:
            deadline = _deadline(timeout)
            self._live(deadline)
            exchange(deadline)
            self._live(deadline)
            self._quiet()
            _remaining(deadline)
            self._complete = complete = True
        except Exception:
            raise AttachExchangeError() from None
        finally:
            if not complete:
                self._retired = True

    def current(self, *, timeout=_DEFAULT_TIMEOUT):
        """Check only this finished local transcript and its unchanged borrow."""
        if current_thread() is not self._thread or not self._complete or self._retired:
            return False
        checked = False
        try:
            deadline = _deadline(timeout)
            self._quiet()
            self._live(deadline)
            self._quiet()
            _remaining(deadline)
            checked = True
            return True
        except Exception:
            return False
        finally:
            if not checked:
                self._retired = True


class ClientExchange(_Exchange):
    def run(self, *, timeout=_DEFAULT_TIMEOUT):
        """Check the offer and send one ACK; no proof the owner consumed it."""
        self._run(self._request, timeout)

    def _request(self, deadline):
        generation = self._witness.generation
        nonce = _challenge()
        self._quiet()
        self._write(_REQUEST + generation + nonce, deadline)
        frame = self._read(_OFFER_SIZE, deadline)
        offer = frame[-_WIDTH:]
        if frame[:-_WIDTH] != _OFFER + generation + nonce or offer in (_ZERO, nonce):
            raise AttachExchangeError()
        self._live(deadline)
        self._quiet()
        self._write(_ACK + generation + nonce + offer, deadline)


class OwnerExchange(_Exchange):
    def run(self, *, timeout=_DEFAULT_TIMEOUT):
        """Offer once and verify ACK on the original counted accepted use."""
        self._run(self._offer, timeout)

    def _offer(self, deadline):
        generation = self._witness.generation
        frame = self._read(_REQUEST_SIZE, deadline)
        nonce = frame[-_WIDTH:]
        if frame[:-_WIDTH] != _REQUEST + generation or nonce == _ZERO:
            raise AttachExchangeError()
        offer = _challenge()
        if offer == nonce:
            raise AttachExchangeError()
        self._live(deadline)
        self._quiet()
        self._write(_OFFER + generation + nonce + offer, deadline)
        if self._read(_ACK_SIZE, deadline) != _ACK + generation + nonce + offer:
            raise AttachExchangeError()
