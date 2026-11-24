#!/usr/bin/env python3
"""Disposable Fedora guest diagnostic, not an uninstall or a host fixture."""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pwd
import re
import stat
import sys
import tarfile
import time
from types import MappingProxyType


SOURCE = Path("/opt/oc-box-uninstall-probe/source")
PERMIT = Path("/run/oc-box-uninstall-probe/permit.json")
HOME = Path("/home/ocbox-probe")
USER = "ocbox-probe"
PODMAN = "/usr/bin/podman"
PURPOSE = "oc-box-uninstall-podman-581"
IDENT = "a" * 32
OWNER = "oc-box-" + IDENT
VOLUME = "oc-box-home-" + IDENT
CONSUMER = "oc-box-probe-stopped-consumer"
IMAGE = "localhost/oc-box-uninstall-probe:fixture"
_TOKEN = re.compile(r"[a-f0-9]{32}")
_SHA = re.compile(r"[a-f0-9]{64}")
_BOOT = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
_PIN = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
_BUDGET = 120
_SHARES = {
    b"9p",
    b"virtiofs",
    b"nfs",
    b"nfs4",
    b"cifs",
    b"smb3",
    b"vboxsf",
    b"fuse.sshfs",
    b"fuse.virtiofs",
    b"fuse.vmhgfs-fuse",
}


class FixtureRefusal(ValueError):
    pass


def _require(condition):
    if not condition:
        raise FixtureRefusal from None


@contextmanager
def _descriptor(path, uid, *, directory=False, leaf_owner=None):
    """No alias or unowned/writable parent can supply a guard or fixture root."""
    path = Path(path)
    _require(
        path.is_absolute()
        and path.anchor == "/"
        and len(path.parts) > 1
        and ".." not in path.parts
    )
    fd = os.open("/", _PIN | os.O_DIRECTORY)
    try:
        for index, name in enumerate(path.parts[1:], 2):
            final = index == len(path.parts)
            child = os.open(
                name,
                _PIN | (os.O_DIRECTORY if directory or not final else 0),
                dir_fd=fd,
            )
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            owners = (0, uid) if not final or leaf_owner is None else (leaf_owner,)
            _require(
                info.st_uid in owners
                and not info.st_mode & 0o022
                and not stat.S_ISLNK(info.st_mode)
            )
        yield fd, info
    finally:
        os.close(fd)


def _identity(info):
    return info.st_dev, info.st_ino


def _file_stamp(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_file(path, uid, limit, *, owner=None, private_permit=False, virtual=False):
    with _descriptor(path, uid, leaf_owner=owner) as (fd, info):
        _require(
            stat.S_ISREG(info.st_mode)
            and info.st_nlink == 1
            and (virtual or info.st_size <= limit)
        )
        if private_permit:
            _require(stat.S_IMODE(info.st_mode) == 0o444)
        with open(f"/proc/self/fd/{fd}", "rb") as stream:
            _require(_file_stamp(os.fstat(stream.fileno())) == _file_stamp(info))
            raw = stream.read(limit + 1)
            _require(
                len(raw) <= limit
                and _file_stamp(os.fstat(stream.fileno())) == _file_stamp(info)
            )
    return raw


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


@dataclass(frozen=True)
class Permit:
    nonce: str
    uid: int
    podman_sha256: str


def _parse_permit(raw, nonce, uid, boot_id, source_hash):
    value = json.loads(raw, object_pairs_hook=_pairs)
    _require(
        type(value) is dict
        and set(value)
        == {
            "version",
            "purpose",
            "nonce",
            "uid",
            "boot_id",
            "fixture_sha256",
            "podman_sha256",
        }
    )
    _require(
        type(value["version"]) is int
        and value["version"] == 1
        and value["purpose"] == PURPOSE
        and type(value["uid"]) is int
        and value["uid"] == uid
        and uid > 0
        and _TOKEN.fullmatch(nonce)
        and value["nonce"] == nonce
        and _BOOT.fullmatch(boot_id)
        and value["boot_id"] == boot_id
        and _SHA.fullmatch(source_hash)
        and value["fixture_sha256"] == source_hash
        and type(value["podman_sha256"]) is str
        and _SHA.fullmatch(value["podman_sha256"])
    )
    return Permit(nonce, uid, value["podman_sha256"])


def _mount_types(raw):
    result = {}
    _require(raw.endswith(b"\n"))
    for line in raw.splitlines():
        left, right = line.split(b" - ", 1)
        fields, filesystem = left.split(), right.split()
        _require(len(fields) >= 6 and len(filesystem) == 3 and fields[0].isdigit())
        ident = int(fields[0])
        _require(
            ident not in result
            and filesystem[0] not in _SHARES
            and not filesystem[0].startswith(b"fuse.")
        )
        result[ident] = filesystem[0]
    return result


def _guest_platform(uid):
    _require(
        not os.path.lexists("/.dockerenv") and not os.path.lexists("/run/.containerenv")
    )
    _require(os.environ.get("OC_BOX_CONTAINER") != "1")
    release = _read_file("/usr/lib/os-release", uid, 8192, owner=0).decode("ascii")
    for key, expected in (("ID", "fedora"), ("VERSION_ID", "44")):
        values = [
            line.partition("=")[2].strip('"')
            for line in release.splitlines()
            if line.startswith(key + "=")
        ]
        _require(values == [expected])
    dmi = "/sys/devices/virtual/dmi/id"
    _require(
        _read_file(dmi + "/sys_vendor", uid, 256, owner=0, virtual=True).strip()
        == b"QEMU"
    )
    product = _read_file(dmi + "/product_name", uid, 256, owner=0, virtual=True).strip()
    _require(product.startswith((b"Standard PC (", b"KVM")))
    proc = f"/proc/{os.getpid()}"
    _require(
        _read_file(proc + "/uid_map", uid, 1024).split() == [b"0", b"0", b"4294967295"]
    )
    with os.scandir("/sys/class/net") as entries:
        loopback = next(entries, None)
        _require(
            loopback is not None
            and loopback.name == "lo"
            and next(entries, None) is None
        )
    return _mount_types(_read_file(proc + "/mountinfo", uid, 512 * 1024))


def _guard(nonce):
    _require(sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode)
    here = Path(__file__)
    _require(here == SOURCE / "tools/podman_uninstall_guest.py")
    uid = os.geteuid()
    _require(uid > 0 and uid == os.getuid())
    account = pwd.getpwuid(uid)
    _require(
        account.pw_name == USER
        and account.pw_dir == str(HOME)
        and account.pw_gid == os.getegid()
        and set(os.getgroups()) <= {account.pw_gid}
    )
    for module in ("__init__.py", "workspace_podman.py", "workspace_uninstall.py"):
        with _descriptor(SOURCE / "src/oc_box" / module, 0, leaf_owner=0) as (_, info):
            _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
    # All source ancestors must be root-controlled, not just the leaf scripts.
    own_bytes = _read_file(here, 0, 128 * 1024, owner=0)
    boot = (
        _read_file("/proc/sys/kernel/random/boot_id", 0, 128, owner=0)
        .decode("ascii")
        .strip()
    )
    permit = _parse_permit(
        _read_file(PERMIT, 0, 2048, owner=0, private_permit=True),
        nonce,
        uid,
        boot,
        hashlib.sha256(own_bytes).hexdigest(),
    )
    return permit, _guest_platform(uid)


def _hash_binary(pin):
    stamp = os.fstat(pin.fd)
    _require(
        4 <= stamp.st_size <= 256 * 1024 * 1024
        and stamp.st_mode & 0o111
        and not stamp.st_mode & 0o6000
    )
    with open(f"/proc/self/fd/{pin.fd}", "rb") as stream:
        header = stream.read(4)
        _require(header == b"\x7fELF")
        digest, left = hashlib.sha256(header), stamp.st_size - len(header)
        while left:
            block = stream.read(min(left, 1024 * 1024))
            _require(block)
            digest.update(block)
            left -= len(block)
        _require(not stream.read(1))
    pin.recheck()
    return digest.hexdigest()


def _reserve(anchor, nonce):
    fd = os.open(
        f"/proc/self/fd/{anchor.fd}", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    )
    try:
        _require(_identity(os.fstat(fd)) == _identity(os.fstat(anchor.fd)))
        with os.scandir(fd) as entries:
            _require(next(entries, None) is None)
        anchor.recheck()
        name = "fixture-" + nonce
        os.mkdir(name, 0o700, dir_fd=anchor.fd)
    finally:
        os.close(fd)
    return HOME / name


def _write(path, content):
    fd = os.open(
        path, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY | os.O_CLOEXEC, 0o600
    )
    with os.fdopen(fd, "wb") as stream:
        stream.write(content if type(content) is bytes else content.encode("ascii"))


def _fresh_settings(root):
    for name in (
        "home",
        "config",
        "cache",
        "data",
        "state",
        "runtime",
        "tmp",
        "graph",
        "graph/volumes",
        "run",
        "static",
        "hooks",
        "project",
        "alias",
    ):
        (root / name).mkdir(mode=0o700)

    def quoted(name):
        return json.dumps(str(root / name))

    _write(
        root / "storage.conf",
        '[storage]\ndriver = "overlay"\n'
        + f"graphroot = {quoted('graph')}\nrunroot = {quoted('run')}\n",
    )
    _write(
        root / "containers.conf",
        "[engine]\nremote = false\n"
        + f"volume_path = {quoted('graph/volumes')}\n"
        + f"static_dir = {quoted('static')}\ntmp_dir = {quoted('tmp')}\n"
        + 'events_logger = "none"\ncgroup_manager = "cgroupfs"\nhooks_dir = []\n'
        "[containers]\nenv = []\nmounts = []\ndevices = []\n",
    )
    _write(root / "registries.conf", "unqualified-search-registries = []\n")
    _write(root / "auth.json", '{"auths":{}}\n')
    _write(root / "mounts.conf", "")
    _write(root / "image.tar", _image_tar())
    return MappingProxyType(
        {
            "HOME": str(root / "home"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
            "TMPDIR": str(root / "tmp"),
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "CONTAINERS_CONF": str(root / "containers.conf"),
            "CONTAINERS_STORAGE_CONF": str(root / "storage.conf"),
            "CONTAINERS_REGISTRIES_CONF": str(root / "registries.conf"),
            "REGISTRY_AUTH_FILE": str(root / "auth.json"),
        }
    )


def _image_tar():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name in ("workspace", "probe-home", "probe-consumer"):
            entry = tarfile.TarInfo(name)
            entry.type, entry.mode = tarfile.DIRTYPE, 0o755
            tar.addfile(entry)
    archive = buffer.getvalue()
    _require(0 < len(archive) <= 16 * 1024)
    return archive


def _resource_id(raw, *, image=False):
    _require(type(raw) is bytes and raw.endswith(b"\n"))
    value = raw[:-1].decode("ascii")
    if image:
        value = value.removeprefix("sha256:")
    _require(_SHA.fullmatch(value))
    return "sha256:" + value if image else value


class GuestEngine:
    """Only these four provisioning actions, then the native read-only parser."""

    def __init__(
        self, native, root, uid, executable, roots, pins, environment, deadline
    ):
        self.native, self.root, self.uid = native, root, uid
        self.executable, self.roots, self.pins = executable, roots, pins
        self.environment, self.deadline = environment, deadline
        self.scope_conf = str(root / "storage.conf")
        self.phase = "preflight"
        self.stage = "verify_empty"
        self._write_index = 0
        self._image = None
        self.prefix = (
            "--remote=false",
            "--root",
            str(root / "graph"),
            "--runroot",
            str(root / "run"),
            "--storage-driver",
            "overlay",
            "--default-mounts-file",
            str(root / "mounts.conf"),
            "--hooks-dir",
            str(root / "hooks"),
        )

    def check(self):
        self.native._remaining(self.deadline)
        for pin in self.pins:
            pin.recheck()

    def _invoke(self, argv, limit, deadline, *, codes=(0,)):
        if not self.native._read_only_query(argv):
            _require(
                self.phase == "provisioning"
                and self._write_index < 4
                and argv == self._write_command(self._write_index)
                and codes == (0,)
            )
            # Consume before transport. No retries on a possibly applied call.
            self._write_index += 1
        self.check()
        result = self.native._capture_pinned(
            self.executable,
            (*self.prefix, *argv),
            self.environment,
            min(deadline, self.deadline),
            limit,
        )
        self.check()
        _require(
            type(result) is tuple
            and len(result) == 2
            and type(result[0]) is int
            and result[0] in codes
            and type(result[1]) is bytes
            and len(result[1]) <= limit
        )
        return result

    def _query(self, argv, deadline, limit, *, codes=(0,)):
        _require(self.native._read_only_query(argv))
        return self._invoke(argv, limit, deadline, codes=codes)

    def _profile(self, containers, images):
        raw = self._query(self.native._INFO, self.deadline, self.native._SMALL)[1]
        data = self.native._json(raw)
        _, count = self.native._bound_profile(data, self.roots, self.scope_conf)
        store = data["store"].get("imageStore")
        _require(
            count == containers
            and type(store) is dict
            and set(store) == {"number"}
            and type(store["number"]) is int
            and store["number"] == images
        )

    def scan(self, *, deadline=None):
        fixture = self
        native = self.native
        _require(self.phase in ("preflight", "read-only"))
        scan_deadline = (
            self.deadline if deadline is None else min(self.deadline, deadline)
        )

        class FixtureInventory(native._LocalInventory):
            def _check(self):
                native._remaining(scan_deadline)
                fixture.check()

            def _info(self, deadline):
                raw = self._query(deadline, native._SMALL, *native._INFO)[1]
                return native._bound_profile(
                    native._json(raw), fixture.roots, fixture.scope_conf
                )

        def capture(executable, argv, environment, deadline, limit):
            _require(executable is self.executable and environment is self.environment)
            _require(argv[:1] == ("--remote=false",))
            codes = (0, 125) if argv[1:] == native._MOUNT_LIST else (0, 1)
            return self._query(
                argv[1:], min(deadline, scan_deadline), limit, codes=codes
            )

        reader = FixtureInventory(
            self.uid,
            self.environment,
            self.executable,
            self.roots,
            self.pins,
            None,
            capture,
        )
        return reader.scan(((OWNER, VOLUME),))

    def _create(self, image, *, consumer=False):
        _require(type(image) is str and re.fullmatch(r"sha256:[a-f0-9]{64}", image))
        args = (
            "container",
            "create",
            "--pull=never",
            "--network=none",
            "--no-healthcheck",
            "--log-driver=none",
            "--image-volume=ignore",
            "--entrypoint=/probe-not-executed",
            "--name",
            CONSUMER if consumer else OWNER,
        )
        if consumer:
            return (
                *args,
                "--requires",
                OWNER,
                "--mount",
                f"type=volume,source={VOLUME},target=/probe-consumer,volume-subpath=nested",
                image,
            )
        return (
            *args,
            "--label",
            f"io.oc-box.workspace={IDENT}",
            "--label",
            "io.oc-box.managed=true",
            "--volume",
            f"{self.root}/project:/workspace:ro",
            "--mount",
            f"type=volume,source={VOLUME},target=/probe-home",
            image,
        )

    def _write_command(self, index):
        if index == 0:
            return "image", "import", str(self.root / "image.tar"), IMAGE
        if index == 1:
            return (
                "volume",
                "create",
                "--driver=local",
                "--label",
                f"io.oc-box.workspace={IDENT}",
                VOLUME,
            )
        return self._create(self._image, consumer=index == 3)

    def provision(self):
        _require(self.phase == "preflight")
        self._profile(0, 0)
        initial = self.scan()
        _require(initial.complete and not initial.labeled_networks)
        self.phase = "provisioning"
        # Import only the pinned synthetic tar, retained on the guest disk.
        self.stage = "synthetic_image"
        self._image = _resource_id(
            self._invoke(self._write_command(0), 256, self.deadline)[1], image=True
        )
        self._profile(0, 1)
        self.stage = "local_home"
        _require(
            self._invoke(self._write_command(1), 256, self.deadline)[1]
            == (VOLUME + "\n").encode()
        )
        self._profile(0, 1)
        self.stage = "volume_subpath"
        raw = self._query(
            ("volume", "inspect", "--", VOLUME), self.deadline, self.native._SMALL
        )[1]
        (item,) = self.native._listed(raw, "Name", (VOLUME,))
        expected = str(self.root / "graph/volumes" / VOLUME / "_data")
        _require(
            item.get("Mountpoint") == expected
            and item.get("Driver") == "local"
            and item.get("Scope") == "local"
            and item.get("Options") == {}
            and item.get("MountCount") == 0
            and type(item.get("MountCount")) is int
            and item.get("Anonymous", False) is False
            and item.get("StorageID", "") == ""
            and self.native._creation_time(item.get("CreatedAt"))
            and item.get("Labels") == {"io.oc-box.workspace": IDENT}
        )
        storage = self.native._pin
        with ExitStack() as stack:
            pin = storage(stack, expected, self.uid, owner=self.uid, directory=True)
            _require(
                pin.stamp[0] == self.roots[2].stamp[0]
                and pin.stamp[2] == self.roots[2].stamp[2]
            )
            parent = storage(
                stack,
                str(Path(expected).parent),
                self.uid,
                owner=self.uid,
                directory=True,
            )
            _require(
                parent.stamp[0] == pin.stamp[0]
                and parent.stamp[2] == pin.stamp[2]
                and len({pin.stamp[:2], parent.stamp[:2], self.roots[2].stamp[:2]}) == 3
            )
            pin.recheck()
            parent.recheck()
            os.mkdir("nested", 0o700, dir_fd=pin.fd)
        self.stage = "stopped_owner"
        owner = _resource_id(
            self._invoke(self._write_command(2), 256, self.deadline)[1]
        )
        self._profile(1, 1)
        self.stage = "stopped_consumer"
        consumer = _resource_id(
            self._invoke(self._write_command(3), 256, self.deadline)[1]
        )
        _require(owner != consumer)
        self.phase = "read-only"
        self.stage = "freeze"
        self._profile(2, 1)
        return owner, consumer


def _alias_negative(native, root, home, uid):
    if home is None or home.mountpoint is None:
        return False, False
    try:
        with ExitStack() as stack:
            volume = stack.enter_context(
                native._path_fd(home.mountpoint.path, uid, directory=True)
            )
            alias = stack.enter_context(
                native._path_fd(str(root / "alias"), uid, directory=True)
            )
            v_identity = _identity(volume[1]) + (native.review._mount_id(volume[0]),)
            a_identity = _identity(alias[1]) + (native.review._mount_id(alias[0]),)
            _require(v_identity == home.mountpoint.identity and alias[1].st_uid == uid)
            return True, v_identity[:2] != a_identity[:2]
    except (OSError, ValueError):
        return False, False


def _report(snapshot, expected, descriptors):
    owners = [row for row in snapshot.containers if row.name == OWNER]
    consumers = [row for row in snapshot.containers if row.name == CONSUMER]
    homes = [row for row in snapshot.volumes if row.name == VOLUME]
    owner = owners[0] if len(owners) == 1 else None
    consumer = consumers[0] if len(consumers) == 1 else None
    home = homes[0] if len(homes) == 1 else None

    def stopped(row):
        return bool(
            row
            and row.status == "created"
            and not row.running
            and not row.paused
            and not row.restarting
            and row.restart_policy == "no"
            and row.pid == row.conmon_pid == 0
        )

    facts = {
        "profile_581": snapshot.profile.version == "5.8.1"
        and snapshot.profile.scope.rootless,
        "exact_container_ids": bool(
            owner
            and consumer
            and len(snapshot.containers) == 2
            and (owner.ident, consumer.ident) == expected
            and owner.workspace_label == IDENT
            and owner.managed
        ),
        "stopped_owner_and_consumer": stopped(owner) and stopped(consumer),
        "stopped_named_subpath_consumer": bool(
            consumer and consumer.volume_refs == ((VOLUME, True),)
        ),
        "stopped_dependency": bool(
            owner and consumer and consumer.dependencies == (owner.ident,)
        ),
        "canonical_local_home": bool(
            home
            and len(snapshot.volumes) == 1
            and home.workspace_label == IDENT
            and home.mount_count == 0
        ),
        "workspace_bind_observed": bool(
            owner
            and owner.bind_entries == 1
            and owner.volume_refs == ((VOLUME, False),)
        ),
        "mounted_catalog_available": bool(
            owner
            and consumer
            and owner.mounted is not None
            and consumer.mounted is not None
        ),
        "both_observed_unmounted": bool(
            owner and consumer and owner.mounted is False and consumer.mounted is False
        ),
        "descriptor_comparison_available": descriptors[0],
        "negative_control_is_distinct": descriptors[1],
        "positive_alias_provisioned": False,
        "positive_mounted_control_provisioned": False,
        "populated_planner_admission": snapshot.complete,
    }
    descriptor_keys = (
        "mounted_catalog_available",
        "both_observed_unmounted",
        "descriptor_comparison_available",
        "negative_control_is_distinct",
    )
    unavailable = {
        *descriptor_keys,
        "positive_alias_provisioned",
        "positive_mounted_control_provisioned",
        "populated_planner_admission",
    }
    if snapshot.complete or not all(facts[key] for key in facts.keys() - unavailable):
        result = "adapter_incomplete"
    elif not all(facts[key] for key in descriptor_keys):
        result = "descriptor_incomplete"
    else:
        result = "complete_diagnostic"
    return {"result": result, **facts}


def _namespace_facts(engine, snapshot, result):
    unavailable = {
        "namespace_probe": "namespace_unavailable",
        "namespace_context_verified": False,
        "namespace_both_observed_unmounted": False,
    }
    if result not in ("complete_diagnostic", "descriptor_incomplete"):
        return {**unavailable, "namespace_probe": "baseline_unverified"}
    try:
        module = SOURCE / "tools/podman_uninstall_namespace.py"
        with _descriptor(module, 0, leaf_owner=0) as (_, info):
            _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
            name = "_ocbox_uninstall_namespace_probe"
            spec = importlib.util.spec_from_file_location(name, module)
            _require(spec is not None and spec.loader is not None)
            podman_uninstall_namespace = importlib.util.module_from_spec(spec)
            sys.modules[name] = podman_uninstall_namespace
            try:
                spec.loader.exec_module(podman_uninstall_namespace)
                return podman_uninstall_namespace.probe(engine, snapshot)
            finally:
                sys.modules.pop(name, None)
    except (Exception, KeyboardInterrupt):
        return {**unavailable, "namespace_preflight_reason": "loader"}


def _run_guest(permit, mounts, *, namespace_diagnostic=False):
    # Trusted archive paths were checked before adding any checkout imports.
    sys.path.insert(0, str(SOURCE / "src"))
    from oc_box import workspace_podman as native

    phase = "guard_refused"
    engine = None
    try:
        with ExitStack() as stack:
            os.umask(0o077)
            binary = native._pin(stack, PODMAN, 0, owner=0, directory=False)
            _require(_hash_binary(binary) == permit.podman_sha256)
            home = native._pin(
                stack, str(HOME), permit.uid, owner=permit.uid, directory=True
            )
            _require(stat.S_IMODE(os.fstat(home.fd).st_mode) == 0o700)
            _require(mounts.get(home.stamp[2]) in (b"btrfs", b"ext4", b"xfs"))
            root = _reserve(home, permit.nonce)
            phase = "provision_incomplete"
            environment = _fresh_settings(root)
            paths = tuple(root / name for name in ("graph", "run", "graph/volumes"))
            roots = tuple(
                native._pin(
                    stack, str(path), permit.uid, owner=permit.uid, directory=True
                )
                for path in paths
            )
            extra = tuple(
                native._pin(
                    stack,
                    str(root / name),
                    permit.uid,
                    owner=permit.uid,
                    directory=directory,
                )
                for name, directory in (
                    ("", True),
                    ("home", True),
                    ("runtime", True),
                    ("config", True),
                    ("cache", True),
                    ("data", True),
                    ("state", True),
                    ("tmp", True),
                    ("static", True),
                    ("hooks", True),
                    ("project", True),
                    ("alias", True),
                    ("containers.conf", False),
                    ("storage.conf", False),
                    ("auth.json", False),
                    ("registries.conf", False),
                    ("mounts.conf", False),
                    ("image.tar", False),
                )
            )
            _require(
                all(
                    pin.stamp[:1] == home.stamp[:1] and pin.stamp[2] == home.stamp[2]
                    for pin in (*roots, *extra)
                )
            )
            engine = GuestEngine(
                native,
                root,
                permit.uid,
                binary,
                roots,
                (binary, home, *roots, *extra),
                environment,
                time.monotonic() + _BUDGET,
            )
            expected = engine.provision()
            phase = "adapter_incomplete"
            snapshot = engine.scan()
            engine._profile(2, 1)
            volume = next((row for row in snapshot.volumes if row.name == VOLUME), None)
            result = _report(
                snapshot, expected, _alias_negative(native, root, volume, permit.uid)
            )
            if namespace_diagnostic:
                result.update(_namespace_facts(engine, snapshot, result["result"]))
            return result
    except (Exception, KeyboardInterrupt):
        if phase == "provision_incomplete":
            return {
                "result": phase,
                "stage": "store_setup" if engine is None else engine.stage,
            }
        return {"result": phase}


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    try:
        _require(
            len(args) in (1, 2)
            and type(args[0]) is str
            and _TOKEN.fullmatch(args[0])
            and (len(args) == 1 or args[1] == "--namespace-diagnostic")
        )
        permit, mounts = _guard(args[0])
        result = _run_guest(permit, mounts, namespace_diagnostic=len(args) == 2)
    except (Exception, KeyboardInterrupt):
        result = {"result": "guard_refused"}
    print(json.dumps(result, sort_keys=True))
    return {"complete_diagnostic": 0, "guard_refused": 2}.get(result["result"], 1)


if __name__ == "__main__":
    raise SystemExit(main())
