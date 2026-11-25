"""Compose admitted source and native host operations for one guarded pass."""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import json
import math
from pathlib import Path
import time
from typing import Callable

if __package__:
    from . import (
        history_overlap_binding,
        history_overlap_domains,
        history_overlap_engine,
        history_overlap_host_scratch,
        history_overlap_io,
        history_overlap_remote,
        history_overlap_writer,
        history_reader,
        opencode_v2_io,
        workspace_control,
        workspace_service,
    )
    from .history_overlap_order import OverlapRefusal
    from .history_overlap_transaction import DestinationCreate, FirstPass
else:
    import history_overlap_binding
    import history_overlap_domains
    import history_overlap_engine
    import history_overlap_host_scratch
    import history_overlap_io
    import history_overlap_remote
    import history_overlap_writer
    import history_reader
    import opencode_v2_io
    import workspace_control
    import workspace_service
    from history_overlap_order import OverlapRefusal
    from history_overlap_transaction import DestinationCreate, FirstPass


@dataclass(frozen=True)
class SourceOperations:
    """Already-bound quiet source operations; every spawn consumes its gate."""

    inventory: Callable
    read: Callable
    begin: Callable
    receive: Callable
    cancel: Callable


@dataclass(repr=False)
class _ReadHandle:
    key: str
    read: history_overlap_io.SourceRead
    witness: history_overlap_remote.RemoteWitness | None = None
    retired: bool | None = None


def _unique_json(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate key")
    return value


def quiet_source_operations(
    box,
    lease,
    data,
    *,
    generation,
    command,
    engine_spawn=None,
    probe,
    cancelled,
    deadline,
):
    """Adapt one exact quiet lease; never substitute an unwitnessed retry."""
    project = box.container_project(data)
    if lease is not workspace_service.quiet_history_source() or (
        lease.connection is not workspace_service.live_connection()
    ):
        raise OverlapRefusal("source-binding")
    endpoint = {key: lease.connection[key] for key in ("url", "password")}
    active = None

    def retire(handle, *, cancel):
        nonlocal active
        if handle.retired is not None:
            return None
        if active is not handle:
            raise OverlapRefusal("read-ownership", cleanup_failed=True)
        handle.retired = False
        proof = None
        try:
            outcome = handle.read.cancel() if cancel else handle.read.finish()
            if outcome.local_cleanup_verified and handle.witness is not None:
                try:
                    proof = handle.witness.observe(deadline=time.monotonic() + 4)
                except history_overlap_remote.RemoteWitnessError:
                    pass
            handle.retired = outcome.remote_cleanup(proof) in (
                "helper-retired",
                "not-started",
            )
            return proof
        except BaseException as error:
            error.history_source_cleanup_failed = True
            raise
        finally:
            try:
                if handle.witness is not None:
                    handle.witness.close()
            finally:
                active = None

    def start(kind, key, *, gate):
        nonlocal active
        if active is not None or lease is not workspace_service.quiet_history_source():
            raise OverlapRefusal("read-ownership")
        attempt = history_overlap_io.SourceAttempt(data["container"], generation)
        child_command = lease.overlap_command(data, attempt)
        argv, env = command(child_command)
        options = {} if engine_spawn is None else engine_spawn(child_command)
        if engine_spawn is not None and (
            set(options) != {"executable", "cwd"} or options["cwd"] != "/"
        ):
            raise OverlapRefusal("source-engine")
        payload = json.dumps(
            {kind: key, "endpoint": endpoint}, separators=(",", ":")
        ).encode()
        try:
            read = history_overlap_io.SourceRead.start(
                argv,
                attempt=attempt,
                env=env,
                payload=payload,
                admission=gate,
                deadline=min(deadline, time.monotonic() + 30),
                max_output=history_overlap_io.MAX_RESPONSE,
                cancelled=cancelled,
                two_phase=True,
                executable=options.get("executable"),
            )
        except (KeyboardInterrupt, SystemExit) as interruption:
            outcome = getattr(interruption, "source_outcome", None)
            if outcome is None or outcome.remote_cleanup() != "not-started":
                interruption.history_source_cleanup_failed = True
            raise
        handle = active = _ReadHandle(key, read)
        try:
            ready = read.wait_ready()
            if ready is None:
                raise OverlapRefusal("source-barrier")
            handle.witness = history_overlap_remote.RemoteWitness.acquire(
                attempt,
                ready,
                probe=probe,
                deadline=min(deadline, time.monotonic() + 4),
            )
            if read.go(handle.witness, admission=gate) is not None:
                raise OverlapRefusal("source-admission")
        except BaseException as error:
            try:
                retire(handle, cancel=True)
            except BaseException as cleanup:
                if isinstance(cleanup, (KeyboardInterrupt, SystemExit)) and isinstance(
                    error, Exception
                ):
                    cleanup.history_source_cleanup_failed = True
                    raise
            if handle.retired is not True:
                error.history_source_cleanup_failed = True
            if isinstance(error, history_overlap_remote.RemoteWitnessError):
                raise OverlapRefusal(
                    "source-witness",
                    cleanup_failed=handle.retired is not True,
                ) from None
            raise
        return handle

    def receive(handle, *, inventory=False):
        proof = retire(handle, cancel=False)
        if handle.retired is not True:
            raise OverlapRefusal("cleanup-unverified", cleanup_failed=True)
        reply = handle.read.response(proof)
        if not reply:
            raise OverlapRefusal("source-response")
        if not inventory and reply in (b"U", b"C"):
            raise box.HistoryPending(
                opencode_v2_io.UNSETTLED if reply == b"U" else opencode_v2_io.CHANGED
            )
        if reply[:1] != (b"I" if inventory else b"E"):
            raise OverlapRefusal("source-response")
        try:
            value = json.loads(reply[1:], object_pairs_hook=_unique_json)
        except (ValueError, RecursionError):
            raise OverlapRefusal("source-response") from None
        if inventory:
            if type(value) is not list:
                raise OverlapRefusal("inventory-binding")
            return value
        if type(value) is not dict or set(value) != {"data"}:
            raise OverlapRefusal("source-response")
        return value["data"]

    def inventory(path, current_data, *, gate):
        values = receive(start("directory", project, gate=gate), inventory=True)
        with gate():
            return box.refresh_sessions(path, current_data, inventory=values)

    def begin(route, *, gate):
        return start("session", route.source, gate=gate)

    def matched(route, handle):
        if type(handle) is not _ReadHandle or route.source != handle.key:
            raise OverlapRefusal("read-ownership", cleanup_failed=True)
        return handle

    def cancel(route, handle):
        handle = matched(route, handle)
        retire(handle, cancel=True)
        return handle.retired is True

    return SourceOperations(
        inventory,
        lambda route, gate: receive(begin(route, gate=gate)),
        begin,
        lambda route, handle: receive(matched(route, handle)),
        cancel,
    )


def bind_first_pass(
    path,
    data,
    *,
    host,
    env,
    domains,
    source,
    stop_token,
    deadline,
    recheck=lambda: None,
    source_generation=None,
    destination_preflight=None,
    destination_failed=None,
):
    """Assemble one guarded pass without acquiring an implicit domain or service.

    The launcher supplies the still-active domain and engine lifetime. The
    coordinator remains on the locking thread, and the backend passes the
    short gate at actual process creation, native dispatch and publication.
    No ambient environment is consulted. Existing holders may finish an
    admitted action; no new action is authorized by a previous check.
    """
    release = data.get("opencode_version")
    lock = workspace_service.LIFECYCLE_LOCK.get()
    if (
        lock is None
        or type(domains) is not history_overlap_domains._Domains
        or type(source) is not SourceOperations
        or type(deadline) not in (int, float)
        or not math.isfinite(deadline)
        or type(env) is not dict
        or not history_overlap_writer.supports(host, release)
    ):
        raise OverlapRefusal("pass-binding")
    # Freeze the chosen environment and recorded identity for this pass.
    env = dict(env)
    ident = data["id"]
    container = data["container"]
    cwd = str(path.parent / "controller")
    host_layout = history_overlap_binding.writable_layout(
        release, env, cwd, role="host"
    )

    def remaining():
        if workspace_service.LIFECYCLE_LOCK.get() is not lock:
            raise OverlapRefusal("pass-lock")
        if time.monotonic() >= deadline:
            raise OverlapRefusal("pass-deadline")
        return deadline

    def current(end):
        remaining()
        if time.monotonic() >= end:
            raise OverlapRefusal("pass-deadline")
        if not workspace_control.unchanged(path, stop_token):
            raise OverlapRefusal("stop-intent")

    @contextmanager
    def gate(end=None, *, source_work=False, preflight=None):
        remaining()
        if end is None:
            end = min(deadline, time.monotonic() + 1)
        else:
            end = min(deadline, end)
        seconds = end - time.monotonic()
        if seconds <= 0:
            raise OverlapRefusal("pass-deadline")
        try:
            with workspace_control.admission(path, timeout=seconds):
                current(end)
                domains.require_destination(host_layout)
                if preflight is not None:
                    preflight(end)
                    current(end)
                    domains.require_destination(host_layout)
                if source_work and source_generation is not None:
                    source_generation(end)
                recheck()
                current(end)
                yield
        except workspace_control.ControlError:
            raise OverlapRefusal("stop-admission") from None

    def match(current_path, current_data):
        remaining()
        if (
            current_path != path
            or current_data.get("id") != ident
            or current_data.get("container") != container
            or current_data.get("opencode_version") != release
        ):
            raise OverlapRefusal("pass-binding")

    def source_gate(end=None):
        return gate(end, source_work=True)

    def inventory(current_path, current_data):
        match(current_path, current_data)
        return source.inventory(current_path, current_data, gate=source_gate)

    def destination(current_path, current_data, route, _archived, _target):
        match(current_path, current_data)

        def guarded_native(operation, *args):
            @contextmanager
            def destination_gate():
                nonlocal spawned
                with gate(preflight=destination_preflight if not spawned else None):
                    yield
                    spawned = True

            spawned = False
            try:
                return operation(
                    host,
                    release,
                    env,
                    cwd,
                    *args,
                    gate=destination_gate,
                    deadline=remaining(),
                )
            except BaseException as error:
                if destination_failed is not None and getattr(
                    error, "history_destination_cleanup_failed", False
                ):
                    destination_failed()
                raise

        def create(candidate, selected):
            if selected != route:
                raise OverlapRefusal("destination-binding")
            return guarded_native(
                history_overlap_writer.create_once, candidate, route.archive
            )

        def readback(selected):
            if selected != route:
                raise OverlapRefusal("destination-binding")
            return guarded_native(history_overlap_writer.readback_once, route.archive)

        return DestinationCreate(create, readback, gate)

    return FirstPass(
        inventory,
        lambda route: source.read(route, gate=source_gate),
        lambda route: source.begin(route, gate=source_gate),
        source.receive,
        source.cancel,
        destination,
    )


@contextmanager
def open_first_pass(
    box,
    path,
    data,
    *,
    binding,
    original,
    host,
    stop_token,
    deadline,
):
    """Join the real quiet lease and host operations under the caller's lifetime.

    The caller must own the verified start and final physical stop on the
    same engine. This scope cannot start or stop the workspace, recreate a
    resource, elect a shared endpoint, or fall back after failure.
    """
    engine = binding.engine
    release = data.get("opencode_version")
    lock = workspace_service.LIFECYCLE_LOCK.get()
    if (
        lock is None
        or type(original) is not history_overlap_engine.Original
        or original._engine is not engine
        or original.container != data.get("container")
        or original.container != binding.container
        or original.mounts != binding.mounts
        or binding.start_verified is not True
        or not history_overlap_writer.supports(host, release)
        or type(deadline) not in (int, float)
        or not math.isfinite(deadline)
    ):
        raise OverlapRefusal("pass-binding")

    def limit(seconds=4):
        bound = min(deadline, time.monotonic() + seconds)
        if time.monotonic() >= bound:
            raise OverlapRefusal("pass-deadline")
        return bound

    def cancelled():
        return (
            workspace_service.LIFECYCLE_LOCK.get() is not lock
            or not workspace_control.unchanged(path, stop_token)
        )

    def check():
        limit()
        engine.recheck()
        original.require_pinned_init()
        if cancelled():
            raise OverlapRefusal("stop-intent")

    @contextmanager
    def lease_spawn():
        end = limit(1)
        try:
            with workspace_control.admission(path, timeout=end - time.monotonic()):
                check()
                history_overlap_domains.verify_prelease_separation(volume, host_layout)
                original.require_running(end)
                check()
                if time.monotonic() >= end:
                    raise OverlapRefusal("pass-deadline")
                yield
        except workspace_control.ControlError:
            raise OverlapRefusal("stop-admission") from None

    def engine_spawn(command):
        if (
            not command
            or command[0] != "podman"
            or command.environment != engine.environment
        ):
            raise OverlapRefusal("source-engine")
        opts = box.bound_history_engine_options(command)
        if set(opts) != {"executable", "cwd"} or opts["cwd"] != "/":
            raise OverlapRefusal("source-engine")
        return opts

    clean = True

    def mark_uncertain():
        nonlocal clean
        clean = False

    with ExitStack() as stack:
        check()
        engine.check_local(limit())
        volume = stack.enter_context(
            history_overlap_binding.pin_quiet_volume(
                box,
                data,
                binding,
                check=check,
                inspect=lambda: original.require_running(limit()),
                inspect_volume=lambda: engine.inspect_volume(
                    data["home_volume"], limit()
                ),
            )
        )
        scratch = stack.enter_context(
            history_overlap_host_scratch.host_archive_scratch(
                path.parent,
                verify_retired=lambda: clean,
            )
        )
        env = history_overlap_writer.writer_environment(
            scratch, box.host_archive_data_home(), release
        )
        cwd = str(path.parent / "controller")
        host_layout = history_overlap_binding.writable_layout(
            release, env, cwd, role="host"
        )

        def destination_preflight(end):
            # A quiet source and disabled policy don't prove a safe fresh host
            # process. Native can wake persisted jobs on startup. Recheck
            # scheduler/blob metadata before spawning each writer/readback.
            history_reader._check_24_host_service(end)
            history_reader._check_24_blobs(Path(host_layout.database).parent, end)
            history_reader._check_database(
                host_layout.database,
                timeout=max(0, end - time.monotonic()),
                version=release,
            )
            if time.monotonic() >= end:
                raise OverlapRefusal("pass-deadline")

        stack.enter_context(
            workspace_service.history_source(
                box,
                data,
                stopped_source=True,
                scratch_binding={
                    **volume.scratch_binding(release),
                    "stock_digest": host.digest,
                },
                engine_spawn=engine_spawn,
                spawn_admission=lease_spawn,
            )
        )
        lease = workspace_service.quiet_history_source()
        if lease is None or lease.source_scope is None:
            raise OverlapRefusal("source-binding")
        mapped, source_layout = stack.enter_context(
            history_overlap_binding.pin_ready_source(
                volume,
                data,
                lease.source_scope,
                top=lambda: original.top(limit()),
                check=check,
            )
        )
        domains = stack.enter_context(
            history_overlap_domains.pin_writable_domains(
                release=release,
                volume=mapped,
                additional_source_backings=(),
                source=source_layout,
                destination=host_layout,
                host_cwd=cwd,
            )
        )
        destination_preflight(limit(1))
        source = quiet_source_operations(
            box,
            lease,
            data,
            generation=original.generation,
            command=lambda command: (command, command.environment),
            engine_spawn=engine_spawn,
            probe=original.probe,
            cancelled=cancelled,
            deadline=deadline,
        )
        yield bind_first_pass(
            path,
            data,
            host=host,
            env=env,
            domains=domains,
            source=source,
            stop_token=stop_token,
            deadline=deadline,
            recheck=check,
            source_generation=original.require_running,
            destination_preflight=destination_preflight,
            destination_failed=mark_uncertain,
        )
