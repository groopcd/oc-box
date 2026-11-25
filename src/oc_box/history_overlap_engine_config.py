"""Pass-local config evidence for the stock rootless Podman history witness.

This admits declarations and checks the corresponding local-store report. It
does not launch Podman, select a different store, or prove effective OCI mounts,
kernel overlay, or the identity of an existing container and its helpers.
"""

from contextlib import ExitStack, contextmanager
import os
import pwd
import stat
from types import MappingProxyType

if __package__:
    from . import workspace_podman
    from .history_overlap_order import OverlapRefusal
else:
    import workspace_podman
    from history_overlap_order import OverlapRefusal


_SYSTEM = "/etc"
_VENDOR = "/usr/share/containers"
_SYSTEM_BIN = "/usr/bin"
_SECRETS = "/usr/share/rhel/secrets"
_FIPS = "/proc/sys/crypto/fips_enabled"
_SYSTEM_UID = 0
_SUBSCRIPTIONS = {
    "etc-pki-entitlement": "pki/entitlement",
    "rhsm": "rhsm",
    "redhat.repo": "yum.repos.d/redhat.repo",
}
_LIMIT = 64 * 1024
_DIR_LIMIT = 64
_ENVIRONMENT = frozenset(
    (
        "HOME",
        "PATH",
        "USER",
        "LOGNAME",
        "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "DBUS_SESSION_BUS_ADDRESS",
        "TMPDIR",
    )
)

# Fedora containers-common common/v0.69.2:
# common/pkg/config/containers.conf, storage/storage.conf,
# common/rpm/{containers-common.spec,00-containers.conf,00-storage.conf,
# 00-storage-rootless.conf}. Empty tables are permitted only at known keys.
# The older 0.67.x storage template's rootful graph/run roots and additional
# image store are intentionally NOT admitted: Podman 6 can apply them rootless.
_ENGINE_VENDOR = {
    "containers": {"log_driver": "journald"},
    "engine": {"runtime": "crun"},
}
_STORAGE_VENDOR = {"storage": {"driver": "overlay"}}
_ROOTLESS_VENDOR = {"storage": {"options": {"overlay": {"mountopt": "nodev"}}}}
_ENGINE_MAIN = {
    "containers": {
        "default_sysctls": ["net.ipv4.ping_group_range=0 0"],
        **_ENGINE_VENDOR["containers"],
    },
    "engine": {
        **_ENGINE_VENDOR["engine"],
        "runtimes": {},
        "runtimes_flags": {},
        "volume_plugins": {},
    },
    "secrets": {"opts": {}},
    "network": {},
    "machine": {},
    "farms": {},
    "podmansh": {},
}
_STORAGE_MAIN = {
    "storage": {
        **_STORAGE_VENDOR["storage"],
        "options": {
            "additionalimagestores": [],
            "overlay": {"mountopt": "nodev"},
            "pull_options": {},
            "vfs": {},
        },
    }
}


def _refuse():
    raise OverlapRefusal("engine-witness") from None


def _require_no_ambient_selectors():
    if any(
        name in workspace_podman._ROUTING_NAMES
        or name.startswith(workspace_podman._ROUTING_PREFIXES)
        or name.startswith("DOCKER_")
        for name in os.environ
    ):
        _refuse()


def _environment(environment, uid):
    # Never normalize an input that would otherwise select a different engine,
    # helper executable, home, session bus, or store. The caller must pass the
    # exact sanitized environment it is going to use throughout the pass.
    if (
        type(uid) is not int
        or uid <= 0
        or not environment.keys() <= _ENVIRONMENT
        or any(
            type(value) is not str or "\0" in value for value in environment.values()
        )
        or environment.get("PATH") != _SYSTEM_BIN
    ):
        _refuse()
    account = pwd.getpwuid(uid)
    home = account.pw_dir
    runtime = f"/run/user/{uid}"
    if (
        environment.get("HOME") != home
        or any(
            environment.get(name, account.pw_name) != account.pw_name
            for name in ("USER", "LOGNAME")
        )
        or environment.get("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
        != f"unix:path={runtime}/bus"
        or environment.get("TMPDIR", "/tmp") != "/tmp"
    ):
        _refuse()
    workspace_podman._select(environment, uid)


def _subset(value, allowed):
    if type(value) is not type(allowed):
        return False
    if type(allowed) is dict:
        return value.keys() <= allowed.keys() and all(
            _subset(item, allowed[key]) for key, item in value.items()
        )
    return value == allowed


def _read(pin, limit=_LIMIT):
    data = workspace_podman._read_pin(pin, limit + 1)
    if len(data) > limit:
        _refuse()
    return data


def _require_toml(pin, allowed, *, exact=False):
    # No extra dependency on Python 3.10; unknown TOML fails closed.
    import tomllib

    decoded = tomllib.loads(_read(pin).decode("utf-8"))
    if not _subset(decoded, allowed) or (exact and decoded != allowed):
        _refuse()


def _absent(path, uid):
    try:
        with workspace_podman._path_fd(path, uid):
            _refuse()
    except FileNotFoundError:
        pass


def _dir_stamp(pin):
    info = os.fstat(pin.fd)
    return info.st_mtime_ns, info.st_ctime_ns


def _names(pin):
    fd = os.open(
        ".",
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=pin.fd,
    )
    try:
        with os.scandir(fd) as entries:
            names = set()
            for entry in entries:
                if len(names) == _DIR_LIMIT:
                    _refuse()
                names.add(entry.name)
            return names
    finally:
        os.close(fd)


class _ConfigWitness:
    def __init__(self, stack, environment, uid):
        self.environment = MappingProxyType(dict(environment))
        self._stack, self._uid = stack, uid
        self._missing = []
        self._pins = []
        self._dirs = []
        self._fips = None
        self._closed = False
        self._failed = False
        home = self.environment["HOME"]
        graph = home + "/.local/share/containers/storage"
        self._roots = (graph, f"/run/user/{uid}/containers", graph + "/volumes")
        self._storage_file = _VENDOR + "/storage.conf"

    def _negative(self, path, *, uid=None):
        if uid is None:
            uid = _SYSTEM_UID
        _absent(path, uid)
        self._missing.append((path, uid))

    def _system_pin(self, path, *, directory=False, required=False):
        try:
            # Require root ownership for the entire system path, not only
            # its final entry.
            pin = workspace_podman._pin(
                self._stack, path, _SYSTEM_UID, owner=_SYSTEM_UID, directory=directory
            )
        except FileNotFoundError:
            if required:
                _refuse()
            self._negative(path)
            return None
        self._pins.append(pin)
        return pin

    def _directory(self, path):
        pin = self._system_pin(path, directory=True)
        if pin is not None:
            self._dirs.append((pin, _dir_stamp(pin)))
        return pin

    def _main(self, path, allowed):
        pin = self._system_pin(path)
        if pin is not None:
            _require_toml(pin, allowed)
        return pin is not None

    def _dropins(self, path, *, allow=None, uid_subdir=False):
        pin = self._directory(path)
        if pin is not None:
            for name in _names(pin):
                if not name.endswith(".conf"):
                    continue
                if allow is None or name != allow[0]:
                    _refuse()
                leaf = self._system_pin(path + "/" + name, required=True)
                _require_toml(leaf, allow[1], exact=True)
        if uid_subdir:
            self._dropins(path + "/" + str(self._uid))

    def _subscriptions(self, site):
        self._negative(site + "/mounts.conf")
        self._fips = self._system_pin(_FIPS)
        if self._fips is not None and _read(self._fips, 8) != b"0\n":
            _refuse()
        mounts = self._system_pin(_VENDOR + "/mounts.conf")
        if mounts is None:
            return
        lines = [
            line
            for line in _read(mounts).decode("ascii").split("\n")
            if line.strip(" \t\r") and not line.lstrip(" \t").startswith("#")
        ]
        if not lines:
            return
        if lines != ["/usr/share/rhel/secrets:/run/secrets"]:
            _refuse()
        # Fedora's known links may be present, but do not grant access to
        # subscription data. Podman follows the links and copies their target
        # bytes; support only missing targets or empty entitlement directories.
        self._system_pin(os.path.dirname(_SECRETS), directory=True)
        secrets = self._directory(_SECRETS)
        if secrets is None:
            return
        for name in _names(secrets):
            if name not in _SUBSCRIPTIONS:
                _refuse()
            relative = _SUBSCRIPTIONS[name]
            info = os.stat(name, dir_fd=secrets.fd, follow_symlinks=False)
            if (
                info.st_uid != _SYSTEM_UID
                or not stat.S_ISLNK(info.st_mode)
                or os.readlink(name, dir_fd=secrets.fd) != "../../../../etc/" + relative
            ):
                _refuse()
            target = _SYSTEM + "/" + relative
            if "/" in relative:
                self._system_pin(os.path.dirname(target), directory=True)
            if name == "redhat.repo":
                self._negative(target)
            else:
                directory = self._directory(target)
                if directory is not None and _names(directory):
                    _refuse()

    def _capture(self):
        site = _SYSTEM + "/containers"
        user = self.environment.get(
            "XDG_CONFIG_HOME", self.environment["HOME"] + "/.config"
        )
        user += "/containers"
        # _path_fd permits a root-owned sticky ancestor (e.g. /tmp). Do not
        # allow one to serve as a system config directory or search prefix.
        vendor_parent = os.path.dirname(_VENDOR)
        for directory in (
            os.path.dirname(vendor_parent),
            vendor_parent,
            _VENDOR,
            _SYSTEM,
            site,
        ):
            self._system_pin(directory, directory=True)
        self._system_pin(_SYSTEM_BIN, directory=True, required=True)

        # The marker is checked by Podman's C startup code, before `info` can
        # report local/remote mode. OCI hooks are a different mechanism.
        self._negative(site + "/podman_preexec_hooks.txt")
        for prefix in (_VENDOR, site):
            self._system_pin(prefix + "/oci", directory=True)
            hooks = self._directory(prefix + "/oci/hooks.d")
            if hooks is not None and any(
                name.endswith(".json") for name in _names(hooks)
            ):
                _refuse()
        self._negative(user + "/oci/hooks.d", uid=self._uid)

        for prefix in (_VENDOR, site, user):
            owner = self._uid if prefix == user else _SYSTEM_UID
            for stem in ("containers", "storage"):
                self._negative(prefix + "/" + stem + ".conf.modules", uid=owner)
                self._negative(prefix + "/" + stem + ".rootless.conf", uid=owner)
                # Preserve the old refusal for nonstandard rootless layouts.
                self._negative(prefix + "/" + stem + ".rootless.d", uid=owner)
        # v5 can read the legacy rootless main and v6 can read vendor/site
        # rootless and per-UID drop-ins. Explicit rootful dirs are not in either
        # rootless search path; they can exist in an unmodified Fedora install.
        for name in (
            "containers.conf",
            "containers.conf.d",
            "containers.rootless.conf.d",
            "storage.conf",
            "storage.conf.d",
            "storage.rootless.conf.d",
            "podman-connections.json",
            "mounts.conf",
        ):
            self._negative(user + "/" + name, uid=self._uid)

        for prefix in (_VENDOR, site):
            self._main(prefix + "/containers.conf", _ENGINE_MAIN)
            if self._main(prefix + "/storage.conf", _STORAGE_MAIN) and prefix == site:
                self._storage_file = prefix + "/storage.conf"
            for suffix, vendor in (
                ("containers.conf.d", ("00-vendor.conf", _ENGINE_VENDOR)),
                ("containers.rootless.conf.d", None),
                ("storage.conf.d", ("00-vendor.conf", _STORAGE_VENDOR)),
                (
                    "storage.rootless.conf.d",
                    ("00-vendor-rootless.conf", _ROOTLESS_VENDOR),
                ),
            ):
                self._dropins(
                    prefix + "/" + suffix,
                    allow=vendor if prefix == _VENDOR else None,
                    uid_subdir=".rootless." in suffix,
                )
        self._subscriptions(site)

    def _check_active(self):
        if self._closed or self._failed:
            _refuse()

    def recheck(self):
        """Check fixed path absences, pinned inodes, and watched directory edits."""
        self._check_active()
        try:
            _require_no_ambient_selectors()
            for path, uid in self._missing:
                _absent(path, uid)
            for pin in self._pins:
                pin.recheck()
            for pin, stamp in self._dirs:
                if _dir_stamp(pin) != stamp:
                    _refuse()
            if self._fips is not None and _read(self._fips, 8) != b"0\n":
                _refuse()
        except (OSError, ValueError):
            self._failed = True
            _refuse()

    def require_local_store(self, info):
        """Validate unforced JSON `podman info` and return roots for caller pins.

        The caller must bracket the native command with recheck(), use exactly
        environment, and pin the three returned paths before resource lookup.
        This shape does not tell kernel overlay apart from automatic FUSE.
        """
        self._check_active()
        try:
            host, store = info["host"], info["store"]
            options = store["graphOptions"]
            if (
                "client" in info
                or host["security"]["rootless"] is not True
                or host["serviceIsRemote"] is not False
                or info["plugins"]["volume"] != ["local"]
                or store["graphDriverName"] != "overlay"
                or store["transientStore"] is not False
                or type(options) is not dict
                or options not in ({}, {"overlay.mountopt": "nodev"})
                or store.get("configFile", self._storage_file) != self._storage_file
                or tuple(store[key] for key in ("graphRoot", "runRoot", "volumePath"))
                != self._roots
            ):
                _refuse()
        except (KeyError, TypeError, ValueError):
            self._failed = True
            _refuse()
        return self._roots


@contextmanager
def pin_config(environment, uid):
    """Pin the selected config inputs without executing an engine or writing.

    `uid` must come from the caller's rootless-host guard. `environment` must
    come from the caller's exact launch selection, with a trusted Fedora
    `/usr/bin` search path, not a rewrite applied just for the witness.
    """
    with ExitStack() as stack:
        try:
            if type(environment) is not dict:
                _refuse()
            environment = environment.copy()
            _require_no_ambient_selectors()
            _environment(environment, uid)
            config = _ConfigWitness(stack, environment, uid)
            config._capture()
            config.recheck()
        except (ImportError, KeyError, OSError, ValueError, RecursionError):
            _refuse()
        try:
            yield config
        finally:
            config._closed = True
