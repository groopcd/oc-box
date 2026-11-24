"""Providerless exec admission for an already registered Studio relay."""

import math
from pathlib import Path
import subprocess
from threading import Event, current_thread
from time import monotonic

if __package__:
    from . import (
        web_guest_duplex_host,
        web_guest_duplex_worker,
        web_guest_worker_drain,
        web_named_native_profile,
        web_studio_guest_binding,
        workspace_control,
    )
else:
    import web_guest_duplex_host
    import web_guest_duplex_worker
    import web_guest_worker_drain
    import web_named_native_profile
    import web_studio_guest_binding
    import workspace_control


class GuestExecError(ValueError):
    def __init__(self):
        super().__init__("The original Studio guest relay could not be admitted.")


def _command(binding):
    port = web_named_native_profile.named_native_port(binding.identity.endpoint)
    owner = binding.owner
    # Read only the installed, fixed worker; neither request paths nor a
    # caller-selected executable, endpoint, or environment reach the command.
    path = Path(web_guest_duplex_worker.__file__)
    with path.open("rb") as stream:
        source = stream.read(65537)
    if not 0 < len(source) <= 65536:
        raise GuestExecError()
    return owner.owner.box.ContainerCommand(
        [
            "podman",
            "exec",
            "-i",
            "--workdir",
            "/",
            binding.identity.container_id,
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            "/usr/bin/python3",
            "-I",
            "-S",
            "-B",
            "-c",
            source.decode("utf-8"),
            str(port),
        ],
        dict(owner._engine_env),
    )


class GuestDuplexExec:
    """Spend one registered tunnel activity, not an OpenCode job receipt.

    The private caller owns the original owner/gateway and must close this
    activity before leaving its worker. Retain the same object on uncertainty.
    Never force local termination or replace a missing transport receipt.
    """

    def __init__(self, binding, workers, use):
        if (
            type(binding)
            not in (
                web_studio_guest_binding._UnservedBinding,
                web_studio_guest_binding._ManagedBinding,
            )
            or type(workers) is not web_guest_worker_drain.AcceptedWorkers
            or type(use) is not web_guest_worker_drain.AcceptedWorker
            or (
                type(binding) is web_studio_guest_binding._ManagedBinding
                and not binding.accepts_registry(workers)
            )
        ):
            raise GuestExecError()
        self._binding = binding
        self._workers = workers
        self._use = use
        self._thread = current_thread()
        self._canceled = Event()
        self._activity = workers.admit(use, wake=self._canceled.set)
        if self._activity is None:
            raise GuestExecError()
        self._spent = self._attempted = self._retired = self._confirmed = False
        self._child = self._tunnel = None

    def _thread_check(self):
        if current_thread() is not self._thread:
            raise GuestExecError()

    def _live(self, deadline):
        binding = self._binding
        return (
            monotonic() < deadline
            and not self._canceled.is_set()
            and self._workers.current(self._use)
            and binding.current() is binding.native.binding
            and not self._canceled.is_set()
            and self._workers.current(self._use)
            and monotonic() < deadline
        )

    def start(self, *, deadline):
        """Return the original captured supervisor once, with no route fallback."""
        self._thread_check()
        if (
            self._retired
            or self._spent
            or type(deadline) not in (int, float)
            or not monotonic() < deadline < math.inf
        ):
            raise GuestExecError()
        self._spent = True
        try:
            if not self._live(deadline):
                raise GuestExecError()
            command = _command(self._binding)
            owner = self._binding.owner.owner
            with workspace_control.admission(
                owner.path, timeout=min(2.0, max(0.0, deadline - monotonic()))
            ):
                if not self._live(deadline):
                    raise GuestExecError()
                # Set before Popen. A startup exception does not prove that no
                # exact exec entered the container.
                self._attempted = True
                self._child = subprocess.Popen(
                    command,
                    env=command.environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                    close_fds=True,
                    cwd="/",
                    start_new_session=True,
                )
                self._tunnel = web_guest_duplex_host.DuplexHost(self._child)
            if not self._live(deadline):
                raise GuestExecError()
            return self._tunnel
        except (OSError, ValueError, RecursionError, subprocess.SubprocessError):
            raise GuestExecError() from None

    def retire(self):
        """Discharge only this activity, with no native work or history claim."""
        self._thread_check()
        if self._retired:
            return self._confirmed
        self._retired = True
        try:
            eligible = not self._attempted or (
                self._tunnel is not None and self._tunnel.close() is True
            )
        except BaseException:
            self._workers.complete(self._use, self._activity, confirmed=False)
            raise
        self._confirmed = self._workers.complete(
            self._use, self._activity, confirmed=eligible
        )
        return self._confirmed
