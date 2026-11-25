"""Bounded read-only Podman witnesses for one guarded quiet-source pass."""

from contextlib import ExitStack, contextmanager
from datetime import datetime
import os
import re
import stat
import time

if __package__:
    from . import (
        history_overlap_domains,
        history_overlap_engine_config,
        history_overlap_remote,
        history_recovery,
        workspace_control,
        workspace_podman,
    )
    from .history_overlap_order import OverlapRefusal
else:
    import history_overlap_domains
    import history_overlap_engine_config
    import history_overlap_remote
    import history_recovery
    import workspace_control
    import workspace_podman
    from history_overlap_order import OverlapRefusal


_LIMIT = 1024 * 1024
_ID = re.compile(r"[a-f0-9]{64}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_GENERATION = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)


def _fail():
    raise OverlapRefusal("engine-witness")


def _one(value):
    if type(value) is not list or len(value) != 1 or type(value[0]) is not dict:
        _fail()
    return value[0]


def _check_pidfd_capability():
    """Reject missing kernel namespace-pin support before asking Podman to start."""
    remote = history_overlap_remote
    if os.uname().sysname != "Linux" or os.uname().machine not in remote._IOCTL_ABIS:
        _fail()
    try:
        with ExitStack() as stack:
            proc = remote._open_proc(stack)
            host = remote._pin(stack, proc, os.getpid())
            sample = host.live()
            if (
                sample[1] != (os.getpid(),)
                or remote._pin_namespaces(stack, host) != sample[2]
                or host.live() != sample
            ):
                _fail()
    except (AttributeError, OSError, ValueError, remote.RemoteWitnessError):
        _fail()


class _Engine:
    def __init__(self, executable, config):
        self.executable = executable
        self.environment = config.environment
        self._config = config
        self.profile = None
        self._roots = ()
        self._closed = False

    def recheck(self):
        if self._closed:
            _fail()
        try:
            self.executable.recheck()
            self._config.recheck()
            for root in self._roots:
                root.recheck()
        except (OSError, workspace_podman.InventoryError):
            _fail()

    def _query(self, argv, deadline, limit):
        try:
            self.recheck()
            status, data = workspace_podman._capture_pinned(
                self.executable, argv, self.environment, deadline, limit
            )
            self.recheck()
            if status != 0:
                _fail()
            return data
        except (OSError, workspace_podman.InventoryError):
            _fail()

    def _json(self, argv, deadline):
        try:
            return workspace_podman._json(self._query(argv, deadline, _LIMIT))
        except workspace_podman.InventoryError:
            _fail()

    def check_local(self, deadline):
        info = self._json(("info", "--format", "json"), deadline)
        profile = self._config.require_local_store(info)
        if self.profile is not None and profile != self.profile:
            _fail()
        return profile

    def inspect(self, container, deadline):
        if type(container) is not str or _NAME.fullmatch(container) is None:
            _fail()
        return _one(self._json(("container", "inspect", "--", container), deadline))

    def inspect_volume(self, volume, deadline):
        if type(volume) is not str or _NAME.fullmatch(volume) is None:
            _fail()
        return _one(self._json(("volume", "inspect", "--", volume), deadline))

    def _top(self, container, deadline, limit):
        if type(container) is not str or _ID.fullmatch(container) is None:
            _fail()
        return self._query(("top", container, "pid", "hpid"), deadline, limit)

    def _transition(self, command, container, deadline):
        if type(container) is not str or _ID.fullmatch(container) is None:
            _fail()
        if command == "start":
            args = ("start", "--", container)
        elif command == "stop":
            args = ("stop", "--time", "5", "--", container)
        else:
            _fail()
        self._query(args, deadline, 4096)

    def command_path(self):
        """Only for the same verified executable while its parent FD is live."""
        self.recheck()
        return f"/proc/{os.getpid()}/fd/{self.executable.fd}"


class Original:
    """Stopped-origin exact resource, with a separately bound running generation."""

    def __init__(self, box, engine, data, deadline):
        self._box, self._engine, self._data = box, engine, dict(data)
        info = engine.inspect(data["container"], deadline)
        ident = info.get("Id")
        if type(ident) is not str or _ID.fullmatch(ident) is None:
            _fail()
        self.container = ident
        self.mounts = self._mounts(info)
        if not history_recovery.verified_container_state(info, running=False):
            _fail()
        self._previous = self._generation(info)
        self._prestart = False
        self._running = None
        self._init = None
        self.start_requested = False

    def _mounts(self, info):
        if info.get("Id") != self.container:
            _fail()
        mounts = self._box._quiet_sync_mounts(self._data, info)
        try:
            passive = history_recovery.passive_workspace_mounts(
                self._box, self._data, info
            )
        except (history_recovery.RecoveryError, KeyError, TypeError):
            _fail()
        if mounts is None or passive != mounts:
            _fail()
        return mounts

    def _generation(self, info):
        try:
            started = info["State"]["StartedAt"]
            if type(started) is not str or _GENERATION.fullmatch(started) is None:
                _fail()
            datetime.fromisoformat(started.replace("Z", "+00:00"))
            return started
        except (KeyError, TypeError, ValueError):
            _fail()

    def _inspect(self, deadline, *, running):
        info = self._engine.inspect(self.container, deadline)
        if self._mounts(
            info
        ) != self.mounts or not history_recovery.verified_container_state(
            info, running=running
        ):
            _fail()
        return info

    def require_before_start(self, deadline):
        info = self._inspect(deadline, running=False)
        if self._running is not None or self._generation(info) != self._previous:
            _fail()
        self._prestart = True
        return info

    def _running_identity(self, info):
        try:
            pid = info["State"]["Pid"]
            if type(pid) is not int or not 1 < pid < 2**31:
                _fail()
            return (
                self._generation(info),
                pid,
                history_overlap_domains.observe_process_start(pid),
            )
        except (KeyError, TypeError, history_overlap_domains.DomainRefusal):
            _fail()

    def bind_started(self, deadline):
        info = self._inspect(deadline, running=True)
        identity = self._running_identity(info)
        if (
            not self._prestart
            or self._running is not None
            or identity[0] == self._previous
        ):
            _fail()
        self._running = identity
        return info

    @property
    def generation(self):
        if self._running is None:
            _fail()
        return self._running[0]

    def require_running(self, deadline):
        info = self._inspect(deadline, running=True)
        if self._running is None or self._running_identity(info) != self._running:
            _fail()
        return info

    def top(self, deadline, limit=history_overlap_remote.MAX_TOP_BYTES):
        self.require_running(deadline)
        contents = self._engine._top(self.container, deadline, limit)
        self.require_running(deadline)
        return contents

    def probe(self, attempt, deadline, limit):
        if (
            attempt.container_id != self.container
            or attempt.generation != self.generation
        ):
            _fail()
        output = self.top(deadline, limit)
        return history_overlap_remote.TopSnapshot(
            attempt,
            self._running[1],
            self._running[2],
            time.monotonic_ns(),
            output,
        )

    def require_stopped(self, deadline):
        """Check stopped metadata for this generation; not physical absence proof."""
        info = self._inspect(deadline, running=False)
        if self._running is None or self._generation(info) != self.generation:
            _fail()
        return info

    def require_pinned_init(self):
        """Check the live kernel identity without a new engine subprocess."""
        if self._init is None:
            _fail()
        init, sample = self._init
        try:
            if init.live() != sample:
                _fail()
        except (OSError, ValueError, history_overlap_remote.RemoteWitnessError):
            _fail()

    def _pin_private_init(self, lifetime):
        remote = history_overlap_remote
        try:
            proc = remote._open_proc(lifetime)
            host = remote._pin(lifetime, proc, os.getpid())
            _host_stat, host_ids, host_namespaces = host.live()
            init = remote._pin(lifetime, proc, self._running[1])
            sample = init.live()
            (_, start), ids, namespaces = sample
            if (
                host_ids != (os.getpid(),)
                or len(ids) <= 1
                or ids[-1] != 1
                or start != self._running[2]
                or any(own == live for own, live in zip(host_namespaces, namespaces))
                or remote._pin_namespaces(lifetime, init) != namespaces
                or init.live() != sample
            ):
                _fail()
        except (OSError, ValueError, remote.RemoteWitnessError):
            _fail()
        self._init = init, sample
        lifetime.callback(setattr, self, "_init", None)

    def start(self, path, stop_token, *, lifetime, deadline):
        """Start the one stopped passive original; never clear an uncertain start."""
        try:
            with workspace_control.admission(path, timeout=1):
                if time.monotonic() >= deadline or not workspace_control.unchanged(
                    path, stop_token
                ):
                    raise OverlapRefusal("stop-intent")
                self.require_before_start(deadline)
                if time.monotonic() >= deadline or not workspace_control.unchanged(
                    path, stop_token
                ):
                    raise OverlapRefusal("stop-intent")
                if not workspace_control.mark_uncertain_if_current(path, stop_token):
                    raise OverlapRefusal("stop-intent")
                self.start_requested = True
                self._engine._transition("start", self.container, deadline)
                info = self.bind_started(deadline)
                self._pin_private_init(lifetime)
                self.require_running(deadline)
                if time.monotonic() >= deadline:
                    _fail()
                workspace_control.finish_transition(path)
                if not workspace_control.unchanged(path, stop_token):
                    raise OverlapRefusal("stop-intent")
                return info
        except workspace_control.ControlError:
            raise OverlapRefusal("stop-admission") from None

    def stop(self, path, *, deadline):
        """Clean up the exact admitted private PID namespace, even after stop intent."""
        if self._init is None:
            raise OverlapRefusal("cleanup-unverified", cleanup_failed=True)
        init, sample = self._init
        remote = history_overlap_remote
        try:
            with workspace_control.admission(path, timeout=1):
                if time.monotonic() >= deadline:
                    _fail()
                if not remote._poll((init.pidfd,)):
                    self.require_running(deadline)
                    if init.live() != sample:
                        _fail()
                    self._engine._transition("stop", self.container, deadline)
                self.require_stopped(deadline)
                remaining = deadline - time.monotonic()
                # Exiting PID 1 tears down a private PID namespace, unlike
                # observing the local Podman wrapper exit. Both generation
                # metadata and this still-pinned kernel task must agree.
                if remaining <= 0 or remote._poll(
                    (init.pidfd,), int(min(remaining, 2) * 1000)
                ) != {init.pidfd}:
                    _fail()
                self.require_stopped(deadline)
        except (KeyboardInterrupt, SystemExit) as interruption:
            interruption.history_source_cleanup_failed = True
            raise
        except (
            OverlapRefusal,
            workspace_control.ControlError,
            OSError,
            ValueError,
            remote.RemoteWitnessError,
        ):
            raise OverlapRefusal("cleanup-unverified", cleanup_failed=True) from None


@contextmanager
def pin_local_witness_engine(environment):
    """Keep the caller's exact sanitized engine selection; require local proof.

    This does not force local/remote flags or start/stop a container. The
    launcher must use this same executable and environment for the original
    lifecycle, including the first name lookup, and positively prove its
    mounted resources. Unknown config is not an implicit routing override.
    """
    if type(environment) is not dict:
        _fail()
    with ExitStack() as stack:
        try:
            uid = workspace_podman._host_uid(environment)
            config = stack.enter_context(
                history_overlap_engine_config.pin_config(environment, uid)
            )
            _check_pidfd_capability()
            # The config witness qualified exactly one immutable search directory.
            # Do not resolve a final symlink (including a remote CLI alias).
            executable = config.environment["PATH"] + "/podman"
            pin = workspace_podman._pin(
                stack, executable, uid, owner=0, directory=False
            )
            info = os.fstat(pin.fd)
            if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
                _fail()
            engine = _Engine(pin, config)
            profile = engine.check_local(time.monotonic() + 5)
            engine._roots = tuple(
                workspace_podman._pin(stack, root, uid, owner=uid, directory=True)
                for root in profile
            )
            engine.profile = profile
            engine.check_local(time.monotonic() + 5)
        except (OSError, workspace_podman.InventoryError):
            _fail()
        try:
            yield engine
        finally:
            engine._closed = True
