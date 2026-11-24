"""Inactive host transport for the exact local OCOR v1 research contract.

The caller supplies one independently reviewed ELF digest and a fully sealed
executable descriptor. This neither selects artifacts nor grants enrollment.
The child must not fork, use other channels, or persist records; descriptor
execution is not a sandbox and does not erase Python memory copies.
"""

import hashlib
import math
import os
import signal
import subprocess
from threading import Lock
from time import monotonic

if __package__:
    from . import web_pake_worker as _pipe
else:
    import web_pake_worker as _pipe


_PROFILE = hashlib.sha256(b"oc-box.named.local/OPAQUE-25519-SHA512-v1").digest()
_SETUP = b"OCOR\x01\x01" + _PROFILE
_RESPOND = b"OCOR\x01\x02" + _PROFILE
_VALIDATE = b"OCOR\x01\x03" + _PROFILE
_SETUP_SIZE = 128
_ID_SIZE = 32
_REQUEST_SIZE = 32
_RESPONSE_SIZE = 64
_RECORD_SIZE = 192


class RegistrationError(Exception):
    """Fixed refusal; do not disclose protocol fields or child diagnostics."""

    def __init__(self):
        super().__init__("Web registration worker refused.")


class RegistrationCleanupError(RegistrationError):
    """Exit could not be verified; the caller must retain its teardown fence."""

    def __init__(self):
        Exception.__init__(
            self, "Web registration worker cleanup could not be verified."
        )


class _Operation(_pipe.PakeWorkerAttempt):
    """Reuse the pipe, pidfd and cancel/reap logic, not the OCOP exchange."""

    def run(self, fd, header, payload, output_size):
        with self._io:
            deadline = self._deadline
            try:
                self._check(deadline)
                _pipe._require_private_reaping()
                # The fixed argv has only argv[0]; OCOR accepts no arguments.
                self._child = _pipe._spawn(fd)
                self._pidfd = os.pidfd_open(self._child.pid)
                os.set_blocking(self._child.stdin.fileno(), False)
                os.set_blocking(self._child.stdout.fileno(), False)
                self._write(header + payload, deadline)
                self._child.stdin.close()
                status = self._status(header, deadline)
                result = (
                    self._exact(output_size, deadline) if status == _pipe._OK else b""
                )
                if (
                    self._read(1, deadline)
                    or self._wait_exit(deadline, cancellable=True) != 0
                    or status != _pipe._OK
                ):
                    raise RegistrationError()
                self._check(deadline)
                return result
            finally:
                self._cleanup(monotonic() + _pipe._REAP_BUDGET)
                if not self._released:
                    raise RegistrationCleanupError() from None


class RegistrationWorker:
    """One local OCOR profile and one live one-shot child at a time.

    ``executable_fd`` is borrowed; the worker owns its sealed duplicate.
    ``expected_sha256`` is the trusted 32 raw digest bytes for exactly that
    artifact. Linux fd-exec/pidfds and sole ownership of child reaping are
    required. Never retry an operation on failure or select another helper.
    Call ``close()`` at teardown. The caller owns any aggregate admission budget.
    """

    def __init__(
        self, executable_fd: int, *, expected_sha256: bytes, io_timeout: float = 2
    ):
        if (
            type(executable_fd) is not int
            or executable_fd < 3
            or not _pipe._width(expected_sha256, 32)
            or type(io_timeout) not in (int, float)
            or not 0 < io_timeout <= 10
        ):
            raise RegistrationError()
        self._lock = Lock()
        self._operation = None
        self._closed = self._fenced = False
        self._timeout = io_timeout
        fd = probe = None
        try:
            _pipe._require_private_reaping()
            if os.execve not in os.supports_fd:
                raise RegistrationError()
            probe = os.pidfd_open(os.getpid())
            signal.pidfd_send_signal(probe, 0)
            fd = _pipe._dup(executable_fd)
            _pipe._verify_executable(fd, expected_sha256)
            self._fd, fd = fd, None
        except (
            AttributeError,
            OSError,
            OverflowError,
            ValueError,
            _pipe.PakeWorkerError,
        ):
            raise RegistrationError() from None
        finally:
            _pipe._close_fd(probe)
            _pipe._close_fd(fd)

    def generate_setup(self, *, deadline: float | None = None) -> bytes:
        """Return 128 private host bytes, never data for a browser response."""
        return self._execute(_SETUP, b"", _SETUP_SIZE, deadline)

    def respond(
        self,
        *,
        setup: bytes,
        credential_identifier: bytes,
        registration_request: bytes,
        deadline: float | None = None,
    ) -> bytes:
        """Answer a blinded request for the trusted host-derived credential ID."""
        if not (
            _pipe._width(setup, _SETUP_SIZE)
            and _pipe._width(credential_identifier, _ID_SIZE)
            and _pipe._width(registration_request, _REQUEST_SIZE)
        ):
            raise RegistrationError()
        return self._execute(
            _RESPOND,
            setup + credential_identifier + registration_request,
            _RESPONSE_SIZE,
            deadline,
        )

    def validate_upload(self, upload: bytes, *, deadline: float | None = None) -> bytes:
        """Parse a canonical upload; this proves neither possession nor scope."""
        if not _pipe._width(upload, _RECORD_SIZE):
            raise RegistrationError()
        return self._execute(_VALIDATE, upload, _RECORD_SIZE, deadline)

    def _execute(self, header, payload, output_size, deadline):
        if deadline is not None and (
            type(deadline) not in (int, float) or not 0 < deadline < math.inf
        ):
            raise RegistrationError()
        cutoff = monotonic() + self._timeout
        if deadline is not None:
            cutoff = min(cutoff, deadline)
        operation = _Operation(self, cutoff)
        fd = None
        try:
            with self._lock:
                if self._fenced:
                    raise RegistrationCleanupError()
                if self._closed or self._operation is not None or monotonic() >= cutoff:
                    raise RegistrationError()
                fd = _pipe._dup(self._fd)
                self._operation = operation
            result = operation.run(fd, header, payload, output_size)
            with self._lock:
                if self._fenced:
                    raise RegistrationCleanupError()
                # At this exact pin an accepted upload serializes to itself.
                if (
                    self._closed
                    or monotonic() >= cutoff
                    or (header == _VALIDATE and result != payload)
                ):
                    raise RegistrationError()
                return result
        except (
            AttributeError,
            OSError,
            RuntimeError,
            ValueError,
            subprocess.SubprocessError,
            _pipe.PakeWorkerError,
        ):
            raise RegistrationError() from None
        finally:
            _pipe._close_fd(fd)

    def _release(self, operation):
        with self._lock:
            if self._operation is operation:
                self._operation = None

    def _fence(self):
        with self._lock:
            self._fenced = True

    def close(self) -> bool:
        """Permanently stop work and report whether reaping was confirmed.

        A later close may recheck the same child only. It never resets the
        fence, replays a request, or starts a replacement.
        """
        deadline = monotonic() + _pipe._DRAIN_BUDGET
        with self._lock:
            self._closed = True
            _pipe._close_fd(self._fd)
            self._fd = None
            operation = self._operation
            if operation is not None:
                operation._cancel.set()
        if operation is not None:
            operation._discard_until(deadline)
        with self._lock:
            return self._operation is None
