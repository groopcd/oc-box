"""Fixture contract tests with synthetic byte output; never spawn Podman."""

from contextlib import contextmanager, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import stat
import tarfile
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import workspace_podman as native
from tools import podman_uninstall_guest as guest
from tools import podman_uninstall_namespace as namespace


_NONCE = "f" * 32
_BOOT = "12345678-4321-5432-a321-0123456789ab"
_HOST_HASH = "b" * 64
_SRC_HASH = "d" * 64
_UID = 2468
_IDS = ("1" * 64, "2" * 64)
_CREATED = "2026-01-02T03:04:05.001Z"
_PERMIT = {
    "version": 1,
    "purpose": guest.PURPOSE,
    "nonce": _NONCE,
    "uid": _UID,
    "boot_id": _BOOT,
    "fixture_sha256": _SRC_HASH,
    "podman_sha256": _HOST_HASH,
}


class FixturePin:
    def __init__(self, path):
        self.path = str(path)
        inode = int.from_bytes(self.path.encode(), "little") % 1000000007 + 7
        self.fd = inode
        self.stamp = (10, inode, 7)
        self.recheck = Mock()


class SyntheticStore:
    def __init__(self):
        self.root = Path("/synthetic/isolated-guest/fixture-" + _NONCE)
        self.roots = tuple(
            FixturePin(self.root / name) for name in ("graph", "run", "graph/volumes")
        )
        self.executable = FixturePin(guest.PODMAN)
        self.names = ()
        self.images = 0
        self.volume_present = False
        self.mount_available = True
        self.volume = {
            "Name": guest.VOLUME,
            "Driver": "local",
            "Scope": "local",
            "Options": {},
            "MountCount": 0,
            "CreatedAt": _CREATED,
            "Labels": {"io.oc-box.workspace": guest.IDENT},
            "Mountpoint": self.roots[2].path + "/" + guest.VOLUME + "/_data",
        }
        self.calls = []
        self.bad_command = None
        self.bad_profile = False
        self.engine = guest.GuestEngine(
            native,
            self.root,
            _UID,
            self.executable,
            self.roots,
            (self.executable, *self.roots),
            {"PATH": "/usr/bin"},
            time.monotonic() + 30,
        )

    def row(self, index):
        ident, name = _IDS[index], self.names[index]
        home = {
            "Type": "volume",
            "Driver": "local",
            "Name": guest.VOLUME,
            "Source": self.volume["Mountpoint"],
            "Destination": "/probe-home" if index == 0 else "/probe-consumer",
        }
        mounts = [home]
        if index == 0:
            mounts.append(
                {
                    "Type": "bind",
                    "Source": str(self.root / "project"),
                    "Destination": "/workspace",
                    "Driver": "",
                }
            )
        else:
            home["SubPath"] = "nested"
        return {
            "Id": ident,
            "Name": name,
            "Created": _CREATED,
            "Driver": "overlay",
            "Namespace": "",
            "Pod": "",
            "Rootfs": "",
            "IsInfra": False,
            "IsService": False,
            "State": {
                "Status": "created",
                "Running": False,
                "Paused": False,
                "Restarting": False,
                "Dead": False,
                "Pid": 0,
            },
            "HostConfig": {"RestartPolicy": {"Name": "no"}},
            "Config": {
                "Labels": {
                    "io.oc-box.workspace": guest.IDENT,
                    "io.oc-box.managed": "true",
                }
                if index == 0
                else None
            },
            "Dependencies": [] if index == 0 else [_IDS[0]],
            "Mounts": mounts,
            "GraphDriver": {
                "Name": "overlay",
                "Data": {
                    "UpperDir": self.roots[0].path
                    + "/overlay/"
                    + "abcd"[index] * 64
                    + "/diff"
                },
            },
        }

    def capture(self, binary, argv, environment, deadline, limit):
        assert binary is self.executable and environment is self.engine.environment
        prefix = self.engine.prefix
        assert argv[: len(prefix)] == prefix
        assert argv[0] == "--remote=false"
        assert 0 < deadline - time.monotonic() <= 30
        cmd = argv[len(prefix) :]
        self.calls.append(cmd)
        if cmd[:2] == self.bad_command:
            return 125, b"SYNTHETIC_RAW_NEVER_PRINT"
        if cmd == native._INFO:
            graph, run, volume = (pin.path for pin in self.roots)
            host = {"security": {"rootless": True}, "serviceIsRemote": False}
            store = {
                "graphRoot": graph if not self.bad_profile else "/foreign/storage",
                "runRoot": run,
                "volumePath": volume,
                "graphDriverName": "overlay",
                "transientStore": False,
                "configFile": self.engine.scope_conf,
                "graphOptions": {},
                "imageStore": {"number": self.images},
                "containerStore": {
                    "number": len(self.names),
                    "running": 0,
                    "paused": 0,
                    "stopped": len(self.names),
                },
            }
            return 0, json.dumps(
                {
                    "host": host,
                    "store": store,
                    "version": {"Version": "5.8.1"},
                    "plugins": {"volume": ["local"]},
                }
            ).encode()
        if cmd in (native._PS, (*native._PS, "--external")):
            return 0, b"".join(
                (ident + "\n").encode() for ident in _IDS[: len(self.names)]
            )
        if cmd == native._VOLUMES:
            return 0, (guest.VOLUME + "\n").encode() if self.volume_present else b""
        if cmd == native._NETWORKS:
            return 0, b""
        if cmd == native._MOUNT_LIST:
            return (0, b"[]") if self.mount_available else (125, b"PRIVATE_MOUNT_ERROR")
        if cmd[:3] == ("volume", "inspect", "--") and cmd[3:] == (guest.VOLUME,):
            return 0, json.dumps([self.volume] if self.volume_present else []).encode()
        if cmd[:3] == ("container", "inspect", "--"):
            return 0, json.dumps([self.row(i) for i in range(len(self.names))]).encode()
        if cmd[:3] == ("container", "exists", "--"):
            return (0 if cmd[3] in self.names else 1), b""
        if cmd == ("volume", "exists", "--", guest.VOLUME):
            return (0 if self.volume_present else 1), b""
        if cmd == ("image", "import", str(self.root / "image.tar"), guest.IMAGE):
            assert limit == 256 and self.images == 0
            self.images = 1
            return 0, ("sha256:" + "f" * 64 + "\n").encode()
        if cmd[:2] == ("volume", "create"):
            assert self.images == 1 and not self.volume_present
            self.volume_present = True
            return 0, (guest.VOLUME + "\n").encode()
        if cmd[:2] == ("container", "create"):
            assert "--pull=never" in cmd and "--network=none" in cmd
            assert cmd[-1] == "sha256:" + "f" * 64 and self.volume_present
            self.names = (*self.names, cmd[cmd.index("--name") + 1])
            return 0, (_IDS[len(self.names) - 1] + "\n").encode()
        raise AssertionError("Unexpected synthetic dispatch")

    @contextmanager
    def mocked(self):
        with (
            patch.object(native, "_capture_pinned", side_effect=self.capture),
            patch.object(
                native,
                "_pin",
                side_effect=lambda _s, path, *_a, **_kw: FixturePin(path),
            ),
            patch.object(guest.os, "mkdir") as mkdir,
        ):
            yield mkdir


class GuestFixtureTests(unittest.TestCase):
    def test_console_uid_cannot_supply_a_dedicated_guest_launch(self):
        console = SimpleNamespace(
            pw_name="ocboxvm", pw_gid=_UID, pw_dir="/home/ocboxvm"
        )
        with (
            patch.object(guest.sys, "flags", SimpleNamespace(isolated=1, no_site=1)),
            patch.object(guest.sys, "dont_write_bytecode", True),
            patch.object(
                guest, "__file__", str(guest.SOURCE / "tools/podman_uninstall_guest.py")
            ),
            patch.object(guest.os, "geteuid", return_value=_UID),
            patch.object(guest.os, "getuid", return_value=_UID),
            patch.object(guest.pwd, "getpwuid", return_value=console),
            patch.object(guest, "_read_file") as read,
        ):
            with self.assertRaises(guest.FixtureRefusal):
                guest._guard(_NONCE)
            read.assert_not_called()

    def test_permit_binds_guest_boot_run_uid_and_archive(self):
        raw = json.dumps(_PERMIT).encode()
        self.assertEqual(
            guest._parse_permit(raw, _NONCE, _UID, _BOOT, _SRC_HASH),
            guest.Permit(_NONCE, _UID, _HOST_HASH),
        )
        for key, value in (
            ("boot_id", "0" * 36),
            ("nonce", "c" * 32),
            ("uid", True),
            ("fixture_sha256", "9" * 64),
            ("podman_sha256", "PRIVATE_VALUE"),
            ("version", 1.0),
        ):
            with self.subTest(key=key):
                with self.assertRaises(guest.FixtureRefusal):
                    guest._parse_permit(
                        json.dumps({**_PERMIT, key: value}).encode(),
                        _NONCE,
                        _UID,
                        _BOOT,
                        _SRC_HASH,
                    )
        duplicate = raw[:-1] + b', "uid": 2468}'
        with self.assertRaises(guest.FixtureRefusal):
            guest._parse_permit(duplicate, _NONCE, _UID, _BOOT, _SRC_HASH)

    def test_guest_checks_platform_nic_namespace_and_share_before_setup(self):
        files = {
            "/usr/lib/os-release": b'ID=fedora\nVERSION_ID="44"\n',
            "sys_vendor": b"QEMU\n",
            "product_name": b"Standard PC (Q35 + ICH9, 2009)\n",
            "uid_map": b"0 0 4294967295\n",
            "mountinfo": b"4 1 1:1 / / rw - btrfs /dev/vda rw\n",
        }
        nic_names = ["lo"]

        @contextmanager
        def nic(_):
            yield iter(SimpleNamespace(name=name) for name in nic_names)

        def read(path, *_args, **_kwargs):
            return files[path if path in files else Path(path).name]

        with (
            patch.object(guest.os.path, "lexists", return_value=False),
            patch.dict(guest.os.environ, {}, clear=True),
            patch.object(guest.os, "scandir", nic),
            patch.object(guest, "_read_file", side_effect=read),
        ):
            self.assertEqual(guest._guest_platform(_UID), {4: b"btrfs"})
            nic_names.append("ens3")
            with self.assertRaises(guest.FixtureRefusal):
                guest._guest_platform(_UID)
            nic_names.pop()
            for key, value in (
                ("uid_map", b"0 1000 1\n"),
                ("sys_vendor", b"Host Vendor\n"),
                ("mountinfo", b"9 1 1:1 / /mnt rw - virtiofs share rw\n"),
            ):
                before, files[key] = files[key], value
                with self.assertRaises(guest.FixtureRefusal):
                    guest._guest_platform(_UID)
                files[key] = before

    def test_fresh_configuration_and_tar_are_only_synthetic_and_refuse_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = guest._fresh_settings(root)
            self.assertEqual(env["XDG_RUNTIME_DIR"], str(root / "runtime"))
            self.assertEqual(env["REGISTRY_AUTH_FILE"], str(root / "auth.json"))
            self.assertEqual((root / "auth.json").read_bytes(), b'{"auths":{}}\n')
            self.assertEqual((root / "mounts.conf").read_bytes(), b"")
            self.assertNotIn("CONTAINER_HOST", env)
            with tarfile.open(root / "image.tar", mode="r:") as archive:
                self.assertEqual(
                    archive.getnames(), ["workspace", "probe-home", "probe-consumer"]
                )
                self.assertTrue(
                    all(item.isdir() and item.size == 0 for item in archive)
                )
            before = (root / "storage.conf").read_bytes()
            with self.assertRaises(FileExistsError):
                guest._fresh_settings(root)
            self.assertEqual((root / "storage.conf").read_bytes(), before)

    def test_fixed_provisioning_then_read_only_native_projection(self):
        store = SyntheticStore()
        with store.mocked() as mkdir:
            ids = store.engine.provision()
            self.assertEqual(ids, _IDS)
            self.assertEqual(store.engine.phase, "read-only")
            mkdir.assert_called_once_with(
                "nested", 0o700, dir_fd=FixturePin(store.volume["Mountpoint"]).fd
            )
            before = len(store.calls)
            snapshot = store.engine.scan()
            self.assertTrue(
                all(native._read_only_query(cmd) for cmd in store.calls[before:])
            )
            self.assertFalse(snapshot.complete)
            self.assertEqual(
                guest._report(snapshot, ids, (True, True))["result"],
                "complete_diagnostic",
            )
            with self.assertRaises(guest.FixtureRefusal):
                store.engine._invoke(
                    store.engine._write_command(0), 256, store.engine.deadline
                )
        mutations = [cmd for cmd in store.calls if not native._read_only_query(cmd)]
        self.assertEqual(
            [cmd[:2] for cmd in mutations],
            [
                ("image", "import"),
                ("volume", "create"),
                ("container", "create"),
                ("container", "create"),
            ],
        )
        self.assertIn("volume-subpath=nested", " ".join(mutations[-1]))
        self.assertIn("--requires", mutations[-1])

    def test_ambiguous_dispatch_not_retried_and_foreign_profile_not_used(self):
        store = SyntheticStore()
        store.bad_profile = True
        with store.mocked():
            with self.assertRaises(native.InventoryError):
                store.engine.provision()
        self.assertEqual(store.calls, [native._INFO])
        store = SyntheticStore()
        store.bad_command = ("image", "import")
        with store.mocked():
            with self.assertRaises(guest.FixtureRefusal):
                store.engine.provision()
            with self.assertRaises(guest.FixtureRefusal):
                store.engine._invoke(
                    store.engine._write_command(0), 256, store.engine.deadline
                )
            with self.assertRaises(guest.FixtureRefusal):
                store.engine._invoke(
                    ("container", "rm", "--", _IDS[0]), 256, store.engine.deadline
                )
        self.assertEqual(
            [cmd[:2] for cmd in store.calls if not native._read_only_query(cmd)],
            [("image", "import")],
        )
        self.assertEqual(store.engine.stage, "synthetic_image")

    def test_descriptor_unavailability_and_adapter_gaps_cannot_be_called_complete(self):
        store = SyntheticStore()
        store.mount_available = False
        with store.mocked():
            ids = store.engine.provision()
            snapshot = store.engine.scan()
        report = guest._report(snapshot, ids, (True, True))
        self.assertEqual(report["result"], "descriptor_incomplete")
        self.assertFalse(report["mounted_catalog_available"])
        self.assertFalse(report["positive_alias_provisioned"])
        self.assertFalse(report["positive_mounted_control_provisioned"])
        self.assertFalse(report["populated_planner_admission"])
        self.assertNotIn("PRIVATE_", json.dumps(report))
        lost = replace(snapshot, volumes=())
        self.assertEqual(
            guest._report(lost, ids, (False, False))["result"], "adapter_incomplete"
        )
        self.assertTrue(
            all(type(value) is bool for key, value in report.items() if key != "result")
        )

    def test_descriptor_control_does_not_confuse_same_inode_with_distinct_directory(
        self,
    ):
        root = Path("/synthetic/guest")
        volume = SimpleNamespace(
            mountpoint=SimpleNamespace(path=str(root / "volume"), identity=(3, 5, 7)),
        )
        separate = True

        @contextmanager
        def guarded(path, uid, *, directory):
            self.assertTrue(directory and uid == _UID)
            if path == volume.mountpoint.path:
                yield 7, SimpleNamespace(st_dev=3, st_ino=5, st_uid=uid)
            else:
                self.assertEqual(path, str(root / "alias"))
                yield (
                    8,
                    SimpleNamespace(st_dev=3, st_ino=6 if separate else 5, st_uid=uid),
                )

        with (
            patch.object(native, "_path_fd", guarded),
            patch.object(native.review, "_mount_id", side_effect=lambda fd: fd),
        ):
            self.assertEqual(
                guest._alias_negative(native, root, volume, _UID), (True, True)
            )
            separate = False
            self.assertEqual(
                guest._alias_negative(native, root, volume, _UID), (True, False)
            )

    def test_main_sanitizes_guard_failure_without_dispatch(self):
        with (
            patch.object(
                guest, "_guard", side_effect=ValueError("PRIVATE_RAW_FAILURE")
            ) as guard,
            patch.object(guest, "_run_guest") as invoke,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(guest.main(["--wrong", "PRIVATE_ARG"]), 2)
            guard.assert_not_called()
            self.assertEqual(guest.main([_NONCE]), 2)
            invoke.assert_not_called()
        self.assertEqual(
            [json.loads(line) for line in output.getvalue().splitlines()],
            [{"result": "guard_refused"}, {"result": "guard_refused"}],
        )

    def test_namespace_diagnostic_is_explicit_and_does_not_change_base_exit(self):
        permit = guest.Permit(_NONCE, _UID, _HOST_HASH)
        with (
            patch.object(guest, "_guard", return_value=(permit, {})),
            patch.object(
                guest, "_run_guest", return_value={"result": "descriptor_incomplete"}
            ) as run,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(guest.main([_NONCE]), 1)
            run.assert_called_once_with(permit, {}, namespace_diagnostic=False)
            run.reset_mock()
            self.assertEqual(guest.main([_NONCE, "--namespace-diagnostic"]), 1)
            run.assert_called_once_with(permit, {}, namespace_diagnostic=True)
            run.reset_mock()
            self.assertEqual(guest.main(["--namespace-diagnostic", _NONCE]), 2)
            run.assert_not_called()
        self.assertEqual(
            guest._namespace_facts(None, None, "adapter_incomplete")["namespace_probe"],
            "baseline_unverified",
        )


class NamespaceProbeTests(unittest.TestCase):
    @contextmanager
    def fixture(self, *, direct=False):
        store = SyntheticStore()
        store.mount_available = direct
        with store.mocked():
            self.assertEqual(store.engine.provision(), _IDS)
            baseline = store.engine.scan()
            yield store, baseline

    def conduct(
        self,
        store,
        baseline,
        *,
        mounts=(b"[]", b"[]"),
        fault=None,
        changed_exit=False,
        fedora=False,
    ):
        commands = []
        anchor = SimpleNamespace(uid=_UID, gid=4711, check=Mock())
        rows = iter(mounts)

        @contextmanager
        def fenced(engine):
            self.assertIs(engine, store.engine)
            yield anchor
            if changed_exit:
                raise OSError("PRIVATE_FENCE_CLOSE")

        def capture(actual_anchor, binary, argv, environment, pins, deadline, limit):
            if fedora:
                self.assertIsInstance(actual_anchor, namespace._Pause)
                self.assertEqual(
                    actual_anchor.executable.path, namespace._FEDORA_CATATONIT
                )
                self.assertEqual(actual_anchor.namespaces, (74, 75))
            else:
                self.assertIs(actual_anchor, anchor)
            self.assertIs(binary, store.executable)
            self.assertEqual(argv[: len(store.engine.prefix)], store.engine.prefix)
            self.assertEqual(
                dict(environment),
                {
                    **store.engine.environment,
                    "_CONTAINERS_USERNS_CONFIGURED": "done",
                    "_CONTAINERS_ROOTLESS_UID": str(_UID),
                    "_CONTAINERS_ROOTLESS_GID": str(_UID) if fedora else "4711",
                },
            )
            self.assertEqual(len(pins), len(store.engine.pins) + 3)
            self.assertGreater(deadline, time.monotonic())
            command = argv[len(store.engine.prefix) :]
            self.assertTrue(native._read_only_query(command))
            commands.append(command)
            if fault is not None:
                value = fault(command)
                if value is not None:
                    return value
            if command == native._MOUNT_LIST:
                return next(rows)
            code, out = store.capture(
                binary, argv, store.engine.environment, deadline, limit
            )
            self.assertEqual(code, 0)
            return out

        report = namespace.probe(
            store.engine,
            baseline,
            fence=namespace._pause_fence if fedora else fenced,
            capture=capture,
        )
        return report, commands, anchor

    @contextmanager
    def pause_window(self, store, *, failure=None, canonical_inode=83):
        uid, pid = _UID, 1234
        runtime = str(store.root / "runtime")
        marker_path = runtime + "/libpod/tmp/pause.pid"
        marker = SimpleNamespace(stamp=(10, 81, 7, uid, 0o100600), recheck=Mock())
        canonical = SimpleNamespace(
            path=namespace._FEDORA_CATATONIT,
            stamp=(10, canonical_inode, 7, 0, 0o100755),
            recheck=Mock(),
        )
        requested = []
        cmdline_reads = 0

        def pin(_stack, path, selected_uid, *, owner, directory):
            requested.append(path)
            if directory:
                self.assertEqual((selected_uid, owner), (uid, uid))
                self.assertTrue(path.startswith(str(store.root / "graph") + "/"))
                return FixturePin(path)
            if path == marker_path:
                self.assertEqual((selected_uid, owner), (uid, uid))
                if failure == "pause_file_missing":
                    raise FileNotFoundError("PRIVATE_PAUSE")
                if failure == "pause_alias":
                    raise native.InventoryError("path-unverified")
                return marker
            self.assertEqual((selected_uid, owner), (0, 0))
            if path == namespace._PAUSE_HELPERS[0]:
                if failure == "mapping_proof":
                    return canonical
                raise native.InventoryError("path-unverified")
            self.assertEqual(path, "/usr/libexec/catatonit/catatonit")
            if failure == "canonical_alias":
                raise native.InventoryError("path-unverified")
            return canonical

        def open_entry(path, *_flags, **_options):
            if path == "ns" and failure == "mapping_proof":
                raise PermissionError("PRIVATE_NAMESPACE")
            return {"/proc": 70, str(pid): 71, "ns": 73, "user": 74, "mnt": 75}[path]

        def metadata(path, **_options):
            if path == "exe":
                if failure == "process_access":
                    raise PermissionError("PRIVATE_EXE")
                return SimpleNamespace(st_dev=10, st_ino=83)
            inode = {
                f"/proc/{pid}": 82,
                "root": 85,
                "user": 74,
                "mnt": 75,
                "/proc/self/ns/user": 100,
                "/proc/self/ns/mnt": 101,
            }[path]
            return SimpleNamespace(st_dev=10, st_ino=inode, st_uid=uid)

        def read_at(fd, name, limit):
            nonlocal cmdline_reads
            self.assertEqual(fd, 71)
            self.assertIn(limit, (64, 2048, 8192))
            if name == "stat":
                return b"1234 (catatonit) S " + b"0 " * 18 + b"42 0\n"
            if name in ("uid_map", "gid_map"):
                outside = uid + int(failure == "fedora_mapping_proof")
                return f"0 {outside} 1\n".encode()
            self.assertEqual(name, "cmdline")
            cmdline_reads += 1
            return (
                b"catatonit\0-P\0extra\0"
                if failure == "wrong_helper_argv"
                or (failure == "argv_drift" and cmdline_reads >= 4)
                else b"catatonit\0-P\0"
            )

        with (
            patch.object(store.engine, "environment", {"XDG_RUNTIME_DIR": runtime}),
            patch.object(native, "_pin", side_effect=pin),
            patch.object(
                native,
                "_read_pin",
                side_effect=lambda pin, _: (
                    (b"01234" if failure == "pause_file_rejected" else b"1234")
                    if pin is marker
                    else (b"NOPE" if failure == "not_elf" else b"\x7fELF")
                ),
            ),
            patch.object(namespace.os, "setns", create=True) as enter,
            patch.object(namespace.os, "supports_fd", {namespace.os.execve}),
            patch.object(namespace.os, "getuid", return_value=uid),
            patch.object(
                namespace.os, "geteuid", return_value=uid + (failure == "prerequisites")
            ),
            patch.object(namespace.os, "getgid", return_value=uid),
            patch.object(namespace.os, "getegid", return_value=uid),
            patch.object(namespace.os, "open", side_effect=open_entry),
            patch.object(namespace.os, "close"),
            patch.object(namespace.os, "stat", side_effect=metadata),
            patch.object(
                namespace.os,
                "fstat",
                side_effect=lambda fd: SimpleNamespace(
                    st_dev=10, st_ino=82 if fd == 71 else fd, st_uid=uid
                ),
            ),
            patch.object(
                namespace.fcntl,
                "ioctl",
                side_effect=lambda fd, _: {74: namespace._USER, 75: namespace._MOUNT}[
                    fd
                ],
            ),
            patch.object(namespace.os, "pidfd_open", return_value=72),
            patch.object(namespace.os, "getpid", return_value=99),
            patch.object(namespace, "_ready", return_value=failure == "pause_gone"),
            patch.object(namespace, "_read_at", side_effect=read_at),
        ):
            yield requested
            enter.assert_not_called()

    def test_preflight_names_are_closed_and_never_dispatch(self):
        with self.fixture() as (store, baseline):
            for failure in (
                "prerequisites",
                "pause_file_missing",
                "pause_file_rejected",
                "pause_alias",
                "process_access",
                "mapping_proof",
            ):
                with (
                    self.subTest(failure=failure),
                    self.pause_window(store, failure=failure) as requested,
                ):
                    capture = Mock()
                    result = namespace.probe(store.engine, baseline, capture=capture)
                    expected = (
                        "pause_file_rejected" if failure == "pause_alias" else failure
                    )
                    self.assertEqual(
                        result,
                        {
                            "namespace_probe": "namespace_unavailable",
                            "namespace_preflight_reason": expected,
                            "namespace_context_verified": False,
                            "namespace_both_observed_unmounted": False,
                        },
                    )
                    self.assertNotIn("/usr/libexec/catatonit/catatonit", requested)
                    capture.assert_not_called()

    def test_wrong_helper_arguments_never_open_fedora_candidate(self):
        with (
            self.fixture() as (store, baseline),
            self.pause_window(store, failure="wrong_helper_argv") as requested,
        ):
            capture = Mock()
            result = namespace.probe(store.engine, baseline, capture=capture)
            self.assertEqual(result["namespace_preflight_reason"], "helper_identity")
            self.assertIs(result["fedora_helper_inode_matches"], False)
            self.assertEqual(len(requested), 1)
            capture.assert_not_called()

    def test_unverified_fedora_executable_never_dispatches(self):
        with self.fixture() as (store, baseline):
            for failure, inode in (
                ("wrong_inode", 99),
                ("canonical_alias", 83),
                ("not_elf", 83),
                ("pause_gone", 83),
            ):
                with (
                    self.subTest(failure=failure),
                    self.pause_window(
                        store, failure=failure, canonical_inode=inode
                    ) as requested,
                ):
                    capture = Mock()
                    result = namespace.probe(store.engine, baseline, capture=capture)
                    self.assertEqual(
                        result,
                        {
                            "namespace_probe": "namespace_unavailable",
                            "namespace_preflight_reason": "helper_identity",
                            "fedora_helper_inode_matches": False,
                            "namespace_context_verified": False,
                            "namespace_both_observed_unmounted": False,
                        },
                    )
                    self.assertEqual(
                        requested[1:],
                        ["/usr/libexec/podman/catatonit"]
                        + (
                            []
                            if failure == "pause_gone"
                            else ["/usr/libexec/catatonit/catatonit"]
                        ),
                    )
                    capture.assert_not_called()

    def test_verified_fedora_pin_uses_existing_namespace_and_store_checks(self):
        with self.fixture() as (store, baseline), self.pause_window(store) as requested:
            original = guest._report(baseline, _IDS, (True, True))
            observed, commands, _ = self.conduct(store, baseline, fedora=True)
            self.assertEqual(
                observed,
                {
                    "namespace_probe": "namespace_observed",
                    "namespace_context_verified": True,
                    "namespace_both_observed_unmounted": True,
                },
            )
            self.assertEqual(
                requested[1:3],
                [
                    "/usr/libexec/podman/catatonit",
                    "/usr/libexec/catatonit/catatonit",
                ],
            )
            self.assertEqual(commands.count(native._MOUNT_LIST), 2)
            self.assertEqual(commands.count(native._INFO), 2)
            self.assertEqual(
                {**original, **observed}["result"], "descriptor_incomplete"
            )
            self.assertFalse(original["populated_planner_admission"])
            with patch.object(
                store.engine, "scan", return_value=replace(baseline, volumes=())
            ):
                drifting, _, _ = self.conduct(store, baseline, fedora=True)
            self.assertEqual(drifting["namespace_probe"], "namespace_unverified")
            self.assertFalse(drifting["namespace_context_verified"])
            self.assertFalse(drifting["namespace_both_observed_unmounted"])

    def test_matching_fedora_helper_cannot_bypass_mapping_or_process_recheck(self):
        with self.fixture() as (store, baseline):
            for failure in ("fedora_mapping_proof", "argv_drift"):
                with (
                    self.subTest(failure=failure),
                    self.pause_window(store, failure=failure) as requested,
                ):
                    capture = Mock()
                    result = namespace.probe(store.engine, baseline, capture=capture)
                    self.assertIn("/usr/libexec/catatonit/catatonit", requested)
                    self.assertEqual(
                        result["namespace_preflight_reason"], "mapping_proof"
                    )
                    self.assertNotIn("fedora_helper_inode_matches", result)
                    self.assertFalse(result["namespace_context_verified"])
                    capture.assert_not_called()

    def test_loader_refusal_is_distinct_from_unadmitted_baseline(self):
        with patch.object(
            guest, "_descriptor", side_effect=PermissionError("PRIVATE_PROBE")
        ) as load:
            baseline = guest._namespace_facts(None, None, "adapter_incomplete")
            load.assert_not_called()
            loader = guest._namespace_facts(None, None, "descriptor_incomplete")
        self.assertEqual(baseline["namespace_probe"], "baseline_unverified")
        self.assertNotIn("namespace_preflight_reason", baseline)
        self.assertEqual(
            loader,
            {
                "namespace_probe": "namespace_unavailable",
                "namespace_preflight_reason": "loader",
                "namespace_context_verified": False,
                "namespace_both_observed_unmounted": False,
            },
        )

    def test_namespace_observation_is_separate_and_rechecks_full_scope(self):
        with self.fixture() as (store, baseline):
            original = guest._report(baseline, _IDS, (True, True))
            self.assertEqual(original["result"], "descriptor_incomplete")
            observed, commands, anchor = self.conduct(store, baseline)
            self.assertEqual(
                observed,
                {
                    "namespace_probe": "namespace_observed",
                    "namespace_context_verified": True,
                    "namespace_both_observed_unmounted": True,
                },
            )
            self.assertEqual(
                {**original, **observed}["result"], "descriptor_incomplete"
            )
            self.assertFalse(original["mounted_catalog_available"])
            self.assertIsNone(baseline.containers[0].mounted)
            self.assertEqual(commands.count(native._MOUNT_LIST), 2)
            self.assertEqual(commands.count(native._INFO), 2)
            self.assertGreaterEqual(anchor.check.call_count, len(commands) * 2)

    def test_blank_malformed_oversized_or_changed_mount_reports_never_mean_absence(
        self,
    ):
        with self.fixture() as (store, baseline):
            data = json.dumps(
                [
                    {
                        "id": _IDS[0],
                        "Names": [guest.OWNER],
                        "mountpoint": store.roots[0].path + "/overlay/a/merged",
                    }
                ]
            ).encode()
            for rows in (
                (b"", b"[]"),
                (b"\n", b"[]"),
                (b"{}", b"[]"),
                (b" " * (native._SMALL + 1), b"[]"),
                (b"[]", data),
            ):
                with self.subTest(rows=tuple(map(len, rows))):
                    observed, _, _ = self.conduct(store, baseline, mounts=rows)
                    self.assertEqual(
                        observed["namespace_probe"], "namespace_unverified"
                    )
                    self.assertFalse(observed["namespace_both_observed_unmounted"])
            observed, _, _ = self.conduct(store, baseline, mounts=(data, data))
            self.assertTrue(observed["namespace_context_verified"])
            self.assertFalse(observed["namespace_both_observed_unmounted"])

    def test_direct_contradiction_and_fence_exit_failure_are_not_replaced(self):
        with self.fixture(direct=True) as (store, baseline):
            data = json.dumps(
                [
                    {
                        "id": _IDS[0],
                        "Names": [guest.OWNER],
                        "mountpoint": store.roots[0].path + "/overlay/a/merged",
                    }
                ]
            ).encode()
            observed, _, _ = self.conduct(store, baseline, mounts=(data, data))
            self.assertFalse(observed["namespace_context_verified"])
            observed, _, _ = self.conduct(store, baseline, changed_exit=True)
            self.assertFalse(observed["namespace_context_verified"])
            self.assertFalse(observed["namespace_both_observed_unmounted"])

    def test_namespace_scope_or_post_observation_change_fails_closed(self):
        with self.fixture() as (store, baseline):

            def fault(cmd):
                if cmd == native._INFO:
                    _, raw = store.capture(
                        store.executable,
                        (*store.engine.prefix, *cmd),
                        store.engine.environment,
                        time.monotonic() + 1,
                        native._SMALL,
                    )
                    info = json.loads(raw)
                    info["store"]["runRoot"] = "/foreign/PRIVATE_STORE"
                    return json.dumps(info).encode()
                return None

            observed, commands, _ = self.conduct(store, baseline, fault=fault)
            self.assertEqual(commands, [native._INFO])
            self.assertEqual(observed["namespace_probe"], "namespace_unverified")
            with patch.object(
                store.engine, "scan", return_value=replace(baseline, volumes=())
            ):
                post, _, _ = self.conduct(store, baseline)
            self.assertEqual(post["namespace_probe"], "namespace_unverified")
            self.assertNotIn("PRIVATE_", json.dumps(observed))

    def test_unavailable_anchor_does_not_dispatch_or_claim_mount_status(self):
        with self.fixture() as (store, baseline):

            def missing(_):
                raise FileNotFoundError("PRIVATE_PAUSE_FILE")

            capture = Mock()
            observed = namespace.probe(
                store.engine, baseline, fence=missing, capture=capture
            )
            capture.assert_not_called()
            self.assertEqual(observed["namespace_probe"], "namespace_unavailable")
            self.assertFalse(observed["namespace_both_observed_unmounted"])

    def test_kernel_mapping_and_pause_process_shapes_are_strict(self):
        normal = b"         0       2468          1\n         1     100000      65536\n"
        self.assertEqual(
            namespace._mappings(normal, _UID), ((0, _UID, 1), (1, 100000, 65536))
        )
        for bad in (
            b"0 0 4294967295\n",
            b"0 2469 1\n",
            b"0 2468 2\n",
            normal + b"2 200000 4\n",
            normal + b"65537 100100 1\n",
        ):
            with self.assertRaises(namespace.ProofRefused):
                namespace._mappings(bad, _UID)
        raw = b"1234 (podman pause) S " + b"0 " * 18 + b"42 0\n"
        self.assertEqual(namespace._process_stamp(raw, 1234), (b"podman pause", 42))
        with self.assertRaises(namespace.ProofRefused):
            namespace._process_stamp(raw, 2222)

    def test_entered_namespace_mismatch_does_not_reach_store_observation(self):
        anchor = SimpleNamespace(namespaces=(73, 74))

        def metadata(path):
            return SimpleNamespace(
                st_dev=10, st_ino=73 if path.endswith("/user") else 99
            )

        with (
            patch.object(namespace.os, "setns", create=True) as enter,
            patch.object(namespace.os, "getresuid", return_value=(0, 0, 0)),
            patch.object(namespace.os, "getresgid", return_value=(0, 0, 0)),
            patch.object(namespace.os, "stat", side_effect=metadata),
            patch.object(
                namespace.os,
                "fstat",
                side_effect=lambda fd: SimpleNamespace(st_dev=10, st_ino=fd),
            ),
            patch.object(namespace.os, "open") as opened,
            patch.object(namespace, "_joined_path") as binding,
        ):
            with self.assertRaises(namespace.ProofRefused):
                namespace._join(anchor, (FixturePin("/synthetic/root"),))
            self.assertEqual(
                [call.args for call in enter.call_args_list],
                [(73, namespace._USER), (74, namespace._MOUNT)],
            )
            opened.assert_not_called()
            binding.assert_not_called()

    def test_transport_refuses_failed_child_and_bounds_output_using_only_direct_pid(
        self,
    ):
        @contextmanager
        def tasks(_):
            yield iter((1,))

        binary = FixturePin("/synthetic/podman")
        for output, status, expect_kill in (
            (b"", [(8765, 125 << 8)], False),
            (b"PRIVATE_OUTPUT", [(0, 0), (8765, 9)], True),
        ):
            with (
                patch.object(namespace.os, "scandir", tasks),
                patch.object(namespace.os, "pipe2", return_value=(41, 42)),
                patch.object(namespace.os, "fdopen", return_value=io.BytesIO()),
                patch.object(namespace.os, "open", return_value=43),
                patch.object(namespace.os, "fork", return_value=8765),
                patch.object(namespace.os, "pidfd_open", return_value=44),
                patch.object(namespace.os, "set_blocking"),
                patch.object(namespace.os, "read", return_value=output),
                patch.object(namespace.os, "close"),
                patch.object(namespace.os, "waitpid", side_effect=status),
                patch.object(namespace.os, "kill") as kill,
                patch.object(namespace, "_ready", return_value=True),
            ):
                with self.assertRaises(namespace.ProofRefused):
                    namespace._capture(
                        None, binary, (), {}, (), time.monotonic() + 2, 4
                    )
                if expect_kill:
                    kill.assert_called_once_with(8765, namespace.signal.SIGKILL)
                else:
                    kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
