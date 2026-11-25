"""Acquire one ordinary quiet-source volume without changing its lifecycle."""

from contextlib import ExitStack, contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat

if __package__:
    from . import (
        file_preview,
        history_overlap_domains,
        history_overlap_remote,
        history_overlap_writer,
        history_recovery,
        runtime_safeguard,
    )
else:
    import file_preview
    import history_overlap_domains
    import history_overlap_remote
    import history_overlap_writer
    import history_recovery
    import runtime_safeguard


_RELEASES = frozenset(("2.4.0", "2.4.1"))


def _refuse():
    raise history_overlap_domains.DomainRefusal("source-backing-unverified")


def _resource(box, data, binding, info, volume):
    if not history_recovery.verified_container_state(info, running=True):
        _refuse()
    mounted = file_preview.verify_roots(box, data, info)
    if mounted is None:
        _refuse()
    ident, (project, home), backing = mounted
    if (
        ident != binding.container
        or (data["project"], project, backing, home) != binding.mounts
        or box._quiet_sync_mounts(data, info) != binding.mounts
        or type(volume) is not dict
        or volume.get("Name") != data["home_volume"]
        or volume.get("Driver") != "local"
        or volume.get("Scope") != "local"
        or volume.get("Mountpoint") != backing
        or volume.get("Options") not in (None, {})
        or type(volume.get("Labels")) is not dict
        or volume["Labels"].get(box.LABEL) != data["id"]
    ):
        _refuse()
    state = info["State"]
    if (
        type(state.get("Pid")) is not int
        or not 1 <= state["Pid"] < 2**31
        or type(state.get("StartedAt")) is not str
        or re.fullmatch(r"[0-9T:.Z+-]{1,128}", state["StartedAt"]) is None
    ):
        _refuse()
    return state["Pid"], state["StartedAt"], home, backing


def _observed_resource(box, data, binding, inspect, inspect_volume):
    try:
        return _resource(box, data, binding, inspect(), inspect_volume())
    except (AttributeError, KeyError, TypeError, OSError):
        _refuse()


def writable_layout(release, env, cwd, *, role):
    """Declare the exact archive profile's roots; do not infer physical backing.

    The known environment is constructed by the corresponding isolated native
    launcher, not copied from the user's live native shell. Unknown overrides
    or an unmatched HOME refuse; the subsequent descriptor proof still checks
    that each scoped directory is present, owned and not aliased.
    """
    if (
        type(release) is not str
        or release not in _RELEASES
        or role not in ("source", "host")
        or type(env) is not dict
        or "OPENCODE_DISABLE_CHANNEL_DB" in env
        or (role == "host" and "OPENCODE_DB" in env)
        or (role == "source" and env.get("OPENCODE_DB") != "oc-box-v2.db")
    ):
        raise history_overlap_domains.DomainRefusal("writable-selector-unverified")

    def selected(name):
        # Reuse the domain path grammar; this only validates a declaration.
        return Path(os.fsdecode(history_overlap_domains._path(env.get(name))))

    home = selected("HOME")
    if selected("OPENCODE_TEST_HOME") != home:
        raise history_overlap_domains.DomainRefusal("writable-selector-unverified")
    data = selected("XDG_DATA_HOME") / "opencode"
    state, cache = selected("XDG_STATE_HOME"), selected("XDG_CACHE_HOME")
    database = data / ("oc-box-v2.db" if role == "source" else "opencode.db")
    roots = (
        ("data", data),
        ("migration", data),
        ("blobs", data),
        ("state", state),
        ("registration", state),
        ("log", data),
        ("cache", cache),
        ("config", selected("XDG_CONFIG_HOME")),
        ("bin", cache),
        ("repos", data),
        ("home-fallback", home / ".local"),
        ("runtime", selected("XDG_RUNTIME_DIR")),
        ("temp", selected("TMPDIR")),
        ("cwd", Path(os.fsdecode(history_overlap_domains._path(cwd)))),
    )
    layout = history_overlap_domains.WritablePaths(
        str(database), tuple((scope, str(path)) for scope, path in roots), (str(home),)
    )
    history_overlap_domains._layout(layout)
    return layout


def source_layout(data, scope):
    """Derive only the launcher-generated source layout; this is not backing proof."""
    release = data.get("opencode_version")
    if (
        type(scope) is not dict
        or set(scope) != {"pid", "cwd"}
        or type(scope["pid"]) is not int
        or not 1 < scope["pid"] < 2**31
        or type(scope["cwd"]) is not str
        or not scope["cwd"].endswith("/work")
    ):
        _refuse()
    home = Path(os.fsdecode(history_overlap_domains._path(data.get("home"))))
    work = Path(os.fsdecode(history_overlap_domains._path(scope["cwd"])))
    root = work.parent
    if (
        root.parent != home
        or re.fullmatch(r"\.oc-box-quiet-history-[a-f0-9]{32}", root.name) is None
    ):
        _refuse()
    env = {
        "HOME": str(root / "home"),
        "OPENCODE_TEST_HOME": str(root / "home"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_STATE_HOME": str(root / "state"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_RUNTIME_DIR": str(root / "runtime"),
        "TMPDIR": str(root / "tmp"),
        "OPENCODE_DB": "oc-box-v2.db",
    }
    return writable_layout(release, env, str(work), role="source")


def _stock_source(space, release):
    """Hash the pinned live native executable, not a path declaration."""
    try:
        fd = os.open(b"exe", os.O_RDONLY | os.O_CLOEXEC, dir_fd=space.target)
        with os.fdopen(fd, "rb") as stream:
            first = runtime_safeguard._signature(os.fstat(stream.fileno()))
            if (
                not stat.S_ISREG(first[2])
                or first[2] & 0o022
                or not 1 <= first[3] <= runtime_safeguard._MAX_BINARY
            ):
                _refuse()
            digest = hashlib.sha256()
            remaining = first[3]
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    _refuse()
                digest.update(chunk)
                remaining -= len(chunk)
            if (
                stream.read(1)
                or runtime_safeguard._signature(os.fstat(stream.fileno())) != first
                or digest.hexdigest() != history_overlap_writer._STOCK.get(release)
            ):
                _refuse()
        space.stock_executable = first
        space.recheck()
    except OSError:
        _refuse()


@contextmanager
def pin_ready_source(volume, data, scope, *, top, check):
    """Acquire the exact native namespace from a still-active quiet lease.

    The caller provides a bounded, fresh, same-engine ``podman top id pid
    hpid`` result (not a cached path reply) and checks the original running
    generation and stop token on both sides. A mapping or a ready record
    alone is never permission to overlap or to rebind the source.
    """
    layout = source_layout(data, scope)
    if type(volume) is not history_overlap_domains._Backing:
        _refuse()
    check()
    volume.recheck()
    try:
        mapping = history_overlap_remote._top(top())
    except history_overlap_remote.RemoteWitnessError:
        _refuse()
    pid = mapping.get(scope["pid"])
    init = volume.source.space
    if pid is None or pid == init.pid or mapping.get(1) != init.pid:
        _refuse()
    tick = history_overlap_domains.observe_process_start(pid)
    with ExitStack() as stack:
        process = stack.enter_context(
            history_overlap_domains.pin_process_namespace(pid, start_ticks=tick)
        )
        if history_overlap_domains._bare_stamp(
            process.ns
        ) != history_overlap_domains._bare_stamp(init.ns):
            _refuse()
        try:
            status = history_overlap_remote._read_leaf(process.target, "status", 65536)
            pids = history_overlap_remote._pid_vector(status, pid)
        except (OSError, ValueError, history_overlap_remote.RemoteWitnessError):
            _refuse()
        if len(pids) < 2 or pids[-1] != scope["pid"]:
            _refuse()
        _stock_source(process, data["opencode_version"])
        check()
        volume.recheck()
        mapped = stack.enter_context(
            history_overlap_domains.pin_mount_backing(
                process, data["home"], volume.host.space, os.fsdecode(volume.host.path)
            )
        )
        if mapped.source.obj != volume.source.obj:
            _refuse()
        yield mapped, layout


@contextmanager
def pin_quiet_volume(box, data, binding, *, inspect, inspect_volume, check):
    """Pin cross-namespace volume backing using read-only engine witnesses.

    The launcher owns the active ordinary lock and exact local engine. The
    two observe callbacks run only against that engine and its already-bound
    original resource. ``check`` rechecks the caller's authority and stop
    token; this function never treats a matching name as authority to select
    or stop a different engine or container.
    """
    release = data.get("opencode_version")
    if (
        type(release) is not str
        or release not in _RELEASES
        or binding is None
        or binding.start_verified is not True
        or data.get("container") != binding.container
    ):
        _refuse()
    check()
    first = _observed_resource(box, data, binding, inspect, inspect_volume)
    pid, _started, home, backing = first
    start = history_overlap_domains.observe_process_start(pid)
    with ExitStack() as stack:
        host = stack.enter_context(history_overlap_domains.pin_current_namespace())
        init = stack.enter_context(
            history_overlap_domains.pin_process_namespace(pid, start_ticks=start)
        )
        volume = stack.enter_context(
            history_overlap_domains.pin_mount_backing(init, home, host, backing)
        )
        check()
        if first != _observed_resource(box, data, binding, inspect, inspect_volume):
            _refuse()
        volume.recheck()
        yield volume
