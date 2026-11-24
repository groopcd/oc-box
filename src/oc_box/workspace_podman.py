"""Pinned local Podman inventory for full uninstall; no mutation transport.

Nonempty engine catalogs are observations, not removal authority. Missing
mounted-container, dependency or source-alias proofs must not be filled in
from Config.Mounts, a container status string, or a lexical source path.
"""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import selectors
import stat
import subprocess
from threading import Lock, current_thread
import time
from types import MappingProxyType

if __package__:
    from . import workspace_uninstall as review
else:
    import workspace_uninstall as review


_LABEL = "io.oc-box.workspace"
_MANAGED = "io.oc-box.managed"
_LIMIT = 128
_BATCH = 8
_SMALL = 64 * 1024
_INSPECT = 2 * 1024 * 1024
_VENDOR = "/usr/share/containers/containers.conf"
_CONFIG_LIMIT = 256 * 1024
_SCAN_SECONDS = 45
_COMMAND_SECONDS = 8
_REAP_SECONDS = 2
_PIN = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
# The repository's native fixture knows this StoreInfo configFile omission
# only for these exact releases. Eligibility here is for read-only inspection;
# none of the populated-store deletion contract is established by the label.
_VERSIONS = ("6.1.4", "6.2.0")
_ROUTING_PREFIXES = (
    "CONTAINER_",
    "CONTAINERS_",
    "_CONTAINERS_",
    "PODMAN_",
    "_PODMAN_",
    "LIBPOD_",
    "STORAGE_",
)
_ROUTING_NAMES = frozenset(
    (
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "XDG_DATA_DIRS",
        "XDG_CONFIG_DIRS",
    )
)
_INFO = ("info", "--format", "json")
_PS = ("ps", "--all", "--quiet", "--no-trunc")
_VOLUMES = ("volume", "ls", "--format", "{{.Name}}")
_NETWORKS = ("network", "ls", "--filter", "label=" + _LABEL, "--format", "{{.Name}}")
_REASONS = frozenset(
    (
        "host-only",
        "unsupported-engine-selector",
        "path-unverified",
        "unsupported-engine-config",
        "engine-timeout",
        "engine-transport-unverified",
        "engine-transport-unreaped",
        "engine-output-limit",
        "malformed-engine-metadata",
        "catalog-unverified",
        "inspect-unverified",
        "engine-binding-unverified",
        "engine-binding-changed",
        "engine-command-unverified",
        "engine-profile-unverified",
        "external-container-store",
        "volume-proof-unavailable",
        "catalog-changed",
        "invalid-expected-resources",
        "engine-inventory-busy",
        "engine-context-closed",
        "engine-inventory-unverified",
        "populated-store-proof-unavailable",
        "engine-executable-unverified",
    )
)


class InventoryError(ValueError):
    """Fixed diagnostic. No raw stdout, selector value, or exception detail."""

    def __init__(self, reason):
        self.reason = (
            reason
            if type(reason) is str and reason in _REASONS
            else "engine-inventory-unverified"
        )
        super().__init__(f"Local Podman inventory refused ({self.reason}).")


def _fail(reason):
    raise InventoryError(reason) from None


@dataclass(frozen=True)
class PodmanProfile:
    version: str
    scope: review.EngineScope


@dataclass(frozen=True)
class ContainerObservation:
    ident: str
    name: str
    workspace_label: str | None
    managed: bool
    status: str
    running: bool
    paused: bool
    restarting: bool
    pid: int
    conmon_pid: int
    mount_entries: int


@dataclass(frozen=True)
class PodmanInventory:
    profile: PodmanProfile
    containers: tuple[ContainerObservation, ...]
    volumes: tuple[review.VolumeInspection, ...]
    labeled_networks: tuple[str, ...]

    @property
    def complete(self):
        # A nonempty store requires native-backed alias, dependency and
        # mounted-resource proofs that this profile has not established.
        return not self.containers and not self.volumes


def _host_uid(environment):
    uid = os.geteuid()
    if (
        uid == 0
        or environment.get("OC_BOX_CONTAINER") == "1"
        or os.path.lexists("/.dockerenv")
        or os.path.lexists("/run/.containerenv")
    ):
        _fail("host-only")
    return uid


def _select(environment, uid):
    if any(key in environment for key in _ROUTING_NAMES) or any(
        key.startswith(_ROUTING_PREFIXES) for key in environment
    ):
        _fail("unsupported-engine-selector")
    home = environment.get("HOME")
    if not review._path(home) or home in ("/", "/home"):
        _fail("unsupported-engine-selector")
    paths = {
        "HOME": home,
        "XDG_CONFIG_HOME": home + "/.config",
        "XDG_DATA_HOME": home + "/.local/share",
        "XDG_STATE_HOME": home + "/.local/state",
        "XDG_CACHE_HOME": home + "/.cache",
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
    }
    if environment.get("XDG_RUNTIME_DIR") != paths["XDG_RUNTIME_DIR"] or any(
        environment.get(key, path) != path for key, path in paths.items()
    ):
        _fail("unsupported-engine-selector")
    return MappingProxyType(
        {**paths, "PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "LANG": "C"}
    )


@contextmanager
def _path_fd(path, uid, *, directory=False):
    """Open path metadata without following any component or reading contents."""
    if not review._path(path) or path == "/":
        _fail("path-unverified")
    parts = Path(path).parts[1:]
    fd = os.open("/", _PIN | os.O_DIRECTORY)
    try:
        for index, name in enumerate(parts):
            parent = index < len(parts) - 1
            child = os.open(
                name, _PIN | (os.O_DIRECTORY if parent or directory else 0), dir_fd=fd
            )
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if (
                info.st_uid not in (0, uid)
                or stat.S_ISLNK(info.st_mode)
                or (
                    info.st_mode & 0o022
                    and not (
                        parent and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
                    )
                )
            ):
                _fail("path-unverified")
        yield fd, info
    finally:
        os.close(fd)


def _stamp(fd, info, directory):
    identity = (info.st_dev, info.st_ino, review._mount_id(fd))
    if directory:
        return identity + (info.st_uid, info.st_mode)
    return identity + review._file_stamp(info)[2:]


@dataclass(frozen=True)
class _Pin:
    path: str
    fd: int
    stamp: tuple[int, ...]
    uid: int
    directory: bool

    def recheck(self):
        with _path_fd(self.path, self.uid, directory=self.directory) as (current, info):
            if (
                _stamp(current, info, self.directory) != self.stamp
                or _stamp(self.fd, os.fstat(self.fd), self.directory) != self.stamp
            ):
                _fail("engine-binding-changed")


def _pin(stack, path, uid, *, owner, directory):
    fd, info = stack.enter_context(_path_fd(path, uid, directory=directory))
    if info.st_uid != owner or (
        not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)
    ):
        _fail("path-unverified")
    return _Pin(path, fd, _stamp(fd, info, directory), uid, directory)


def _no_overrides(home_config, uid):
    # Do not silently override an existing selection by forcing --remote=false.
    # Refuse site/user configuration, modules, drop-ins and saved connections;
    # these profiles need an independently audited merge/selector contract.
    paths = (
        "/usr/share/containers/containers.conf.d",
        "/usr/share/containers/containers.conf.modules",
        "/usr/share/containers/containers.rootless.conf",
        "/usr/share/containers/containers.rootless.d",
        "/usr/share/containers/storage.conf",
        "/etc/containers/containers.conf",
        "/etc/containers/containers.conf.d",
        "/etc/containers/containers.conf.modules",
        "/etc/containers/containers.rootless.conf",
        "/etc/containers/containers.rootless.d",
        "/etc/containers/storage.conf",
        *(
            home_config + "/containers/" + name
            for name in (
                "containers.conf",
                "containers.conf.d",
                "containers.conf.modules",
                "containers.rootless.conf",
                "containers.rootless.d",
                "storage.conf",
                "podman-connections.json",
            )
        ),
    )
    for path in paths:
        try:
            with _path_fd(path, uid):
                _fail("unsupported-engine-config")
        except FileNotFoundError:
            pass


def _read_pin(pin, limit):
    with open(f"/proc/self/fd/{pin.fd}", "rb") as stream:
        return stream.read(limit)


def _vendor_pin(stack, uid):
    try:
        pin = _pin(stack, _VENDOR, uid, owner=0, directory=False)
    except FileNotFoundError:
        return None
    # Python 3.10 has no standard TOML parser; fail closed if a file is present.
    try:
        import tomllib

        content = _read_pin(pin, _CONFIG_LIMIT + 1)
        if len(content) > _CONFIG_LIMIT:
            _fail("unsupported-engine-config")
        configuration = tomllib.loads(content.decode("utf-8"))
    except (ImportError, ValueError, RecursionError):
        _fail("unsupported-engine-config")
    engine = configuration.get("engine", {})
    if (
        type(engine) is not dict
        or engine.get("remote", False) is not False
        or engine.get("active_service") not in (None, "")
        or engine.get("service_destinations") not in (None, {})
        or any(
            engine.get(key) not in (None, "")
            for key in (
                "static_dir",
                "tmp_dir",
                "volume_path",
                "namespace",
                "database_backend",
            )
        )
    ):
        _fail("unsupported-engine-config")
    pin.recheck()
    return pin


def _vendor_unchanged(pin, uid):
    if pin is not None:
        pin.recheck()
        return
    try:
        with _path_fd(_VENDOR, uid):
            _fail("engine-binding-changed")
    except FileNotFoundError:
        pass


def _remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        _fail("engine-timeout")
    return seconds


def _read_only_query(argv):
    if type(argv) is not tuple or not all(type(arg) is str for arg in argv):
        return False
    if argv in (_INFO, _PS, (*_PS, "--external"), _VOLUMES, _NETWORKS):
        return True
    if not 4 <= len(argv) <= _BATCH + 3 or argv[2] != "--":
        return False
    kind, verb = argv[:2]
    if kind not in ("container", "volume"):
        return False
    if verb == "inspect":
        pattern = review._ENGINE_ID if kind == "container" else review._RESOURCE_NAME
        return all(review._matches(pattern, item) for item in argv[3:])
    pattern = (
        review._WORKSPACE_CONTAINER if kind == "container" else review._WORKSPACE_VOLUME
    )
    return verb == "exists" and len(argv) == 4 and review._matches(pattern, argv[3])


def _private_capture(executable, argv, environment, deadline, limit):
    """Hard stdout cap, no disk capture, no stderr, and bounded direct reap."""
    if argv[:1] != ("--remote=false",) or not _read_only_query(argv[1:]):
        _fail("engine-command-unverified")
    if type(limit) is not int or not 0 < limit <= _INSPECT:
        _fail("engine-output-limit")
    deadline = min(deadline, time.monotonic() + _COMMAND_SECONDS)
    _remaining(deadline)
    process = None
    try:
        process = subprocess.Popen(
            (executable.path, *argv),
            executable=f"/proc/self/fd/{executable.fd}",
            pass_fds=(executable.fd,),
            env=environment,
            cwd="/",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        output = bytearray()
        fd = process.stdout.fileno()
        os.set_blocking(fd, False)
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while True:
                if not selector.select(_remaining(deadline)):
                    _fail("engine-timeout")
                try:
                    chunk = os.read(fd, min(65536, limit + 1 - len(output)))
                except BlockingIOError:
                    continue
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > limit:
                    _fail("engine-output-limit")
        return process.wait(timeout=_remaining(deadline)), bytes(output)
    except subprocess.TimeoutExpired:
        _fail("engine-timeout")
    except (OSError, ValueError) as error:
        if isinstance(error, InventoryError):
            raise
        _fail("engine-transport-unverified")
    finally:
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=_REAP_SECONDS)
            except (OSError, subprocess.SubprocessError, KeyboardInterrupt):
                _fail("engine-transport-unreaped")
            finally:
                if process.stdout is not None:
                    process.stdout.close()


def _json(raw):
    try:
        return json.loads(
            raw,
            object_pairs_hook=review._unique_pairs,
            parse_constant=review._bad_constant,
        )
    except (ValueError, RecursionError):
        _fail("malformed-engine-metadata")


def _catalog(raw, pattern):
    if not raw:
        return ()
    if not raw.endswith(b"\n"):
        _fail("catalog-unverified")
    try:
        rows = raw[:-1].decode("ascii").split("\n")
    except UnicodeError:
        _fail("catalog-unverified")
    if (
        len(rows) > _LIMIT
        or len(set(rows)) != len(rows)
        or not all(pattern.fullmatch(row) for row in rows)
    ):
        _fail("catalog-unverified")
    return tuple(sorted(rows))


def _listed(raw, key, expected):
    items = _json(raw)
    if type(items) is not list or len(items) != len(expected):
        _fail("inspect-unverified")
    indexed = {}
    for item in items:
        ident = item.get(key) if type(item) is dict else None
        if type(ident) is not str or ident not in expected or ident in indexed:
            _fail("inspect-unverified")
        indexed[ident] = item
    return tuple(indexed[name] for name in expected)


def _labels(value):
    if value is None:
        return None, False
    if type(value) is not dict:
        _fail("inspect-unverified")
    owner, managed = value.get(_LABEL), value.get(_MANAGED)
    if (owner is not None and not review._matches(review._WORKSPACE_ID, owner)) or (
        managed is not None and managed not in ("true", "false")
    ):
        _fail("inspect-unverified")
    return owner, managed == "true"


def _container(item):
    name, state, config, mounts = (
        item.get(key) for key in ("Name", "State", "Config", "Mounts")
    )
    if (
        not review._matches(review._RESOURCE_NAME, name)
        or type(state) is not dict
        or type(config) is not dict
        or type(mounts) is not list
        or len(mounts) > review.MAX_MOUNTS
        or any(type(mount) is not dict for mount in mounts)
        or state.get("Status")
        not in (
            "configured",
            "created",
            "exited",
            "stopped",
            "running",
            "paused",
            "stopping",
        )
        or any(
            type(state.get(key)) is not bool
            for key in ("Running", "Paused", "Restarting")
        )
        or any(
            type(state.get(key)) is not int or not 0 <= state[key] < 2**31
            for key in ("Pid", "ConmonPid")
        )
        or "Labels" not in config
    ):
        _fail("inspect-unverified")
    owner, managed = _labels(config["Labels"])
    return ContainerObservation(
        item["Id"],
        name,
        owner,
        managed,
        state["Status"],
        state["Running"],
        state["Paused"],
        state["Restarting"],
        state["Pid"],
        state["ConmonPid"],
        len(mounts),
    )


class _LocalInventory:
    def __init__(self, uid, environment, executable, roots, pinned, vendor, capture):
        self._uid = uid
        self._environment = environment
        self._executable = executable
        self._roots = roots
        self._pinned = pinned
        self._vendor = vendor
        self._capture = capture
        self._thread = current_thread()
        self._latch = Lock()
        self._closed = False
        self._profile = self._info(time.monotonic() + _COMMAND_SECONDS)

    @property
    def profile(self):
        return self._profile

    def _check(self):
        _no_overrides(self._environment["XDG_CONFIG_HOME"], self._uid)
        _vendor_unchanged(self._vendor, self._uid)
        for pin in self._pinned:
            pin.recheck()

    def _query(self, deadline, limit, *argv, codes=(0,)):
        if not _read_only_query(argv):
            _fail("engine-command-unverified")
        try:
            _remaining(deadline)
            self._check()
            actual_deadline = min(deadline, time.monotonic() + _COMMAND_SECONDS)
            _remaining(actual_deadline)
            result = self._capture(
                self._executable,
                ("--remote=false", *argv),
                self._environment,
                actual_deadline,
                limit,
            )
            _remaining(actual_deadline)
            self._check()
            _remaining(actual_deadline)
        except InventoryError:
            raise
        except Exception:
            _fail("engine-binding-unverified")
        if (
            type(result) is not tuple
            or len(result) != 2
            or type(result[0]) is not int
            or result[0] not in codes
            or type(result[1]) is not bytes
        ):
            _fail("engine-command-unverified")
        if len(result[1]) > limit:
            _fail("engine-output-limit")
        return result

    def _info(self, deadline):
        info = _json(self._query(deadline, _SMALL, *_INFO)[1])
        if type(info) is not dict:
            _fail("engine-profile-unverified")
        host, store, version = (info.get(key) for key in ("host", "store", "version"))
        if not all(type(value) is dict for value in (host, store, version)):
            _fail("engine-profile-unverified")
        security = host.get("security")
        native = version.get("Version")
        if (
            type(security) is not dict
            or security.get("rootless") is not True
            or host.get("serviceIsRemote") is not False
            or type(native) is not str
            or native not in _VERSIONS
            or store.get("graphDriverName") != "overlay"
            or store.get("transientStore") is not False
            or "configFile" in store
            or tuple(store.get(k) for k in ("graphRoot", "runRoot", "volumePath"))
            != tuple(pin.path for pin in self._roots)
        ):
            _fail("engine-profile-unverified")
        roots = tuple(
            review.StoragePath(pin.path, pin.stamp[:3]) for pin in self._roots
        )
        scope = review.EngineScope(True, False, *roots)
        if not review._storage_layout(scope):
            _fail("engine-profile-unverified")
        return PodmanProfile(native, scope)

    def _catalogs(self, deadline):
        def ids(*extra):
            return _catalog(
                self._query(deadline, _SMALL, *_PS, *extra)[1],
                review._ENGINE_ID,
            )

        principal = ids()
        if ids("--external") != principal:
            _fail("external-container-store")
        volumes = _catalog(
            self._query(deadline, _SMALL, *_VOLUMES)[1],
            review._RESOURCE_NAME,
        )
        networks = _catalog(
            self._query(deadline, _SMALL, *_NETWORKS)[1],
            review._RESOURCE_NAME,
        )
        return principal, volumes, networks

    def _volume(self, item):
        required = (
            "Name",
            "Driver",
            "Scope",
            "Labels",
            "Mountpoint",
            "Options",
            "StorageID",
            "Anonymous",
            "CreatedAt",
            "MountCount",
        )
        if any(key not in item for key in required):
            _fail("volume-proof-unavailable")
        name, root = item["Name"], self._roots[2]
        path = root.path + "/" + name + "/_data"
        count = item["MountCount"]
        if (
            item["Driver"] != "local"
            or item["Scope"] != "local"
            or item["Options"] not in (None, {})
            or item["StorageID"] not in (None, "")
            or item["Anonymous"] is not False
            or item["Mountpoint"] != path
            or not review._valid_created(item["CreatedAt"])
            or type(count) is not int
            or not 0 <= count < 2**31
        ):
            _fail("volume-proof-unavailable")
        owner, _ = _labels(item["Labels"])
        # No recursive store traversal and no read of volume contents.
        with ExitStack() as stack:
            parent = _pin(
                stack,
                str(Path(path).parent),
                self._uid,
                owner=self._uid,
                directory=True,
            )
            data = _pin(stack, path, self._uid, owner=self._uid, directory=True)
            if (
                any(
                    pin.stamp[0] != root.stamp[0]
                    or pin.stamp[2] != root.stamp[2]
                    or pin.stamp[:2] == root.stamp[:2]
                    for pin in (parent, data)
                )
                or parent.stamp[:2] == data.stamp[:2]
            ):
                _fail("volume-proof-unavailable")
            data.recheck()
            parent.recheck()
        return review.VolumeInspection(
            name,
            owner,
            "local",
            "local",
            False,
            True,
            False,
            item["CreatedAt"],
            review.StoragePath(path, data.stamp[:3]),
            count,
        )

    def _observations(self, deadline, kind, expected, key, project):
        output = []
        for index in range(0, len(expected), _BATCH):
            chunk = expected[index : index + _BATCH]
            response = self._query(deadline, _INSPECT, kind, "inspect", "--", *chunk)[1]
            output.extend(project(item) for item in _listed(response, key, chunk))
        return tuple(output)

    def _presence(self, deadline, expected, containers, volumes):
        cnames = {item.name for item in containers}
        vnames = {item.name for item in volumes}
        for container, volume in expected:
            for kind, name, catalog in (
                ("container", container, cnames),
                ("volume", volume, vnames),
            ):
                result, _ = self._query(
                    deadline, _SMALL, kind, "exists", "--", name, codes=(0, 1)
                )
                if (result == 0) != (name in catalog):
                    _fail("catalog-changed")

    def scan(self, names):
        """Bounded partial observations, never raw native content."""
        if (
            type(names) is not tuple
            or len(names) > _LIMIT
            or any(
                type(pair) is not tuple
                or len(pair) != 2
                or not review._matches(review._WORKSPACE_CONTAINER, pair[0])
                or not review._matches(review._WORKSPACE_VOLUME, pair[1])
                or pair[0][-32:] != pair[1][-32:]
                for pair in names
            )
            or len(set(names)) != len(names)
        ):
            _fail("invalid-expected-resources")
        if current_thread() is not self._thread or not self._latch.acquire(
            blocking=False
        ):
            _fail("engine-inventory-busy")
        try:
            if self._closed:
                _fail("engine-context-closed")
            deadline = time.monotonic() + _SCAN_SECONDS
            if self._info(deadline) != self.profile:
                _fail("engine-binding-changed")
            before = self._catalogs(deadline)
            containers = self._observations(
                deadline, "container", before[0], "Id", _container
            )
            if len({item.name for item in containers}) != len(containers):
                _fail("inspect-unverified")
            volumes = self._observations(
                deadline, "volume", before[1], "Name", self._volume
            )
            self._presence(deadline, names, containers, volumes)
            if self._catalogs(deadline) != before:
                _fail("catalog-changed")
            if self._info(deadline) != self.profile:
                _fail("engine-binding-changed")
            return PodmanInventory(self.profile, containers, volumes, before[2])
        except InventoryError:
            raise
        except Exception:
            _fail("engine-inventory-unverified")
        finally:
            self._latch.release()

    def inspect(self, names):
        found = self.scan(names)
        if not found.complete:
            _fail("populated-store-proof-unavailable")
        return review.EngineSnapshot(
            found.profile.scope, (), (), found.labeled_networks, True
        )


@contextmanager
def local_inventory(executable, *, environment=None, capture=None):
    """Bind the no-override default local overlay profile, then yield a reader.

    Production callers choose an explicit canonical packaged ELF ``podman``
    path. The optional capture callback is a trusted synthetic seam; it gets
    only the pinned executable, sanitized environment, and bounded commands.
    This context exposes no rm dispatch capability.
    """
    environment = os.environ if environment is None else environment
    uid = _host_uid(environment)
    selected = _select(environment, uid)
    try:
        path = os.fspath(executable)
    except TypeError:
        _fail("engine-executable-unverified")
    if not review._path(path) or Path(path).name != "podman":
        _fail("engine-executable-unverified")
    with ExitStack() as stack:
        try:
            _no_overrides(selected["XDG_CONFIG_HOME"], uid)
            vendor = _vendor_pin(stack, uid)
            binary = _pin(stack, path, uid, owner=0, directory=False)
            if (
                not binary.stamp[4] & 0o111
                or binary.stamp[4] & 0o6000
                or _read_pin(binary, 4) != b"\x7fELF"
            ):
                _fail("engine-executable-unverified")
            bases = tuple(
                _pin(stack, selected[key], uid, owner=uid, directory=True)
                for key in ("HOME", "XDG_RUNTIME_DIR")
            )
            graph = selected["XDG_DATA_HOME"] + "/containers/storage"
            roots = tuple(
                _pin(stack, item, uid, owner=uid, directory=True)
                for item in (
                    graph,
                    selected["XDG_RUNTIME_DIR"] + "/containers",
                    graph + "/volumes",
                )
            )
            reader = _LocalInventory(
                uid,
                selected,
                binary,
                roots,
                (binary, *bases, *roots),
                vendor,
                _private_capture if capture is None else capture,
            )
        except InventoryError:
            raise
        except Exception:
            _fail("engine-binding-unverified")
        try:
            yield reader
        finally:
            # Don't close/recycle the executable FD while a query is using it,
            # including exceptional teardown requested by another thread.
            with reader._latch:
                reader._closed = True
