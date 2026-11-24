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


if __name__ == "__main__":
    unittest.main()
