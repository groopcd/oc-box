import copy
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch

from oc_box import guarded_devices as devices, oc_box as box


KVM = "/dev/kvm"
NODES = ["/dev/accel/accel0", "/dev/dri/renderD128", KVM]
MAPPINGS = [*NODES[:-1], f"{KVM}:rw"]
IDENT = "a" * 64
ENGINE = {"PATH": "/usr/bin", "CONTAINERS_CONF": "/synthetic/local.conf"}
GRAPH = Path("/synthetic/containers")
RUN = Path("/synthetic/run-containers")
BUNDLE = GRAPH / "overlay-containers" / IDENT / "userdata"
RUNDIR = RUN / "overlay-containers" / IDENT / "userdata"


class GuardedDevicesTests(unittest.TestCase):
    def setUp(self):
        self.data = {"devices": MAPPINGS.copy()}
        self.info = {
            "Id": IDENT,
            "HostConfig": {"Privileged": False, "Devices": []},
            "Config": {
                "CreateCommand": [
                    "podman", "create", *(f"--device={mapping}" for mapping in MAPPINGS)
                ]
            },
            "OCIConfigPath": str(BUNDLE / "config.json"),
        }
        self.declared = {
            "host_device_list": [
                {"path": mapping, "type": "", "major": 0, "minor": 0}
                for mapping in MAPPINGS
            ]
        }
        self.oci = {
            "mounts": [
                {
                    "type": "bind",
                    "source": path,
                    "destination": path,
                    "options": ["rw", "rbind", "slave", "nosuid", "noexec"],
                }
                for path in NODES
            ]
            + [
                {
                    "type": "bind",
                    "source": str(BUNDLE / "shm"),
                    "destination": "/dev/shm",
                    "options": ["rw"],
                }
            ]
        }

    def verify(self, *, fresh=False, signature=(1, 2, 3)):
        storage = {
            "host": {"serviceIsRemote": False, "security": {"rootless": True}},
            "store": {
                "graphRoot": str(GRAPH),
                "runRoot": str(RUN),
                "graphDriverName": "overlay",
            },
        }

        def respond(*args, **kwargs):
            self.assertEqual(args[0], "--remote=false")
            self.assertEqual(kwargs["env"], ENGINE)
            self.assertTrue(kwargs["private"])
            if args[1] == "info":
                self.assertEqual(args[1:], ("info", "--format=json"))
                data = storage
            else:
                self.assertEqual(
                    args[1:], ("generate", "spec", "--name=false", "--compact", IDENT)
                )
                data = self.declared
            return SimpleNamespace(returncode=0, stdout=json.dumps(data))

        with (
            patch.object(box, "podman", side_effect=respond) as podman,
            patch.object(devices, "_node", return_value=signature) as stat,
            patch.object(devices, "_read_bundle", return_value=self.oci) as bundle,
        ):
            result = devices.verify(box, self.data, self.info, ENGINE, fresh=fresh)
        self.assertEqual(podman.call_count, 2)
        self.assertEqual(stat.call_count, 2 * len(NODES))
        if fresh and not self.info.get("OCIConfigPath"):
            bundle.assert_not_called()
        else:
            bundle.assert_called_once_with(BUNDLE / "config.json")
        return result

    def test_exact_nodes_are_bound(self):
        self.assertEqual(self.verify(), tuple((path, 1, 2, 3) for path in NODES))

    def test_rejects_broader_device_forms_and_duplicate_paths(self):
        for value in (
            ["/dev/dri"],
            ["/dev/dri/renderD128:/dev/dri/renderD129"],
            ["/dev/accel/accel0:r"],
            ["vendor/class=node"],
            ["/dev/dri/../dri/renderD128"],
            ["/dev/dri/renderD128"] * 2,
            ["/dev/mem"],
            ["/dev/kvm"],
            ["/dev/kvm:r"],
            ["/dev/kvm:rwm"],
            ["/dev/kvm:/dev/kvm:rw"],
            ["/dev/kvm:rw"] * 2,
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(devices.DeviceError, "saved plain"),
            ):
                devices.paths({"devices": value})

    def test_stopped_source_cannot_substitute_create_argv_for_an_effective_bundle(self):
        del self.info["OCIConfigPath"]
        with self.assertRaisesRegex(devices.DeviceError, "previously generated"):
            self.verify()
        self.assertEqual(len(self.verify(fresh=True)), len(NODES))

    def test_kvm_requires_exact_saved_mapping_in_independent_podman_metadata(self):
        create = self.info["Config"]["CreateCommand"]
        create[-1] = "--device=/dev/kvm"
        with patch.object(box, "podman") as podman:
            with self.assertRaisesRegex(devices.DeviceError, "do not match"):
                devices.verify(box, self.data, self.info, ENGINE)
            podman.assert_not_called()
        create[-1] = "--device=/dev/kvm:rw"
        declared_kvm = self.declared["host_device_list"][-1]
        declared_kvm["path"] = "/dev/kvm"
        with self.assertRaisesRegex(devices.DeviceError, "do not match"):
            self.verify()
        declared_kvm["path"] = "/dev/kvm:rw"
        kvm_mount = self.oci["mounts"][len(NODES) - 1]
        kvm_mount["destination"] = "/dev/other"
        with self.assertRaisesRegex(devices.DeviceError, "do not match"):
            self.verify()

    def test_kvm_checks_direct_linux_device_identity_without_opening_for_io(self):
        kvm = os.makedev(10, 232)
        for mode, rdev, allowed in (
            (stat.S_IFCHR, kvm, True),
            (stat.S_IFCHR, os.makedev(1, 3), False),
            (stat.S_IFLNK, kvm, False),
        ):
            info = SimpleNamespace(st_mode=mode, st_dev=1, st_ino=2, st_rdev=rdev)
            with (
                self.subTest(mode=mode, rdev=rdev),
                patch.object(devices.os, "open", side_effect=[10, 11]) as open_node,
                patch.object(devices.os, "fstat", return_value=info),
                patch.object(devices.os, "close") as close,
            ):
                if allowed:
                    self.assertEqual(devices._node(KVM), (1, 2, kvm))
                else:
                    with self.assertRaisesRegex(devices.DeviceError, "direct character"):
                        devices._node(KVM)
                self.assertEqual(
                    open_node.call_args_list,
                    [
                        call(
                            "/dev",
                            os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                        ),
                        call("kvm", os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=10),
                    ],
                )
                self.assertEqual(close.call_args_list, [call(11), call(10)])

    def test_refuses_missing_extra_or_changed_effective_bindings(self):
        expected = copy.deepcopy(self.oci)
        for mutation in (
            lambda mounts: mounts.pop(0),
            lambda mounts: mounts.append({**mounts[0]}),
            lambda mounts: mounts[0].update(source="/dev/accel/accel9"),
            lambda mounts: mounts[0]["options"].append("ro"),
            lambda mounts: mounts[0]["options"].remove("nosuid"),
            lambda mounts: mounts.append(
                {
                    "type": "bind",
                    "source": "/dev/dri/card0",
                    "destination": "/dev/dri/card0",
                    "options": [],
                }
            ),
        ):
            with self.subTest(mutation=mutation):
                self.oci = copy.deepcopy(expected)
                mutation(self.oci["mounts"])
                with self.assertRaisesRegex(devices.DeviceError, "do not match"):
                    self.verify()

    def test_refuses_missing_declarations_or_extra_host_requests(self):
        self.declared["host_device_list"].pop()
        with self.assertRaisesRegex(devices.DeviceError, "do not match"):
            self.verify(fresh=True)
        self.info["HostConfig"]["DeviceRequests"] = [{}]
        with patch.object(box, "podman") as podman:
            with self.assertRaisesRegex(devices.DeviceError, "do not match"):
                devices.verify(box, self.data, self.info, ENGINE)
            podman.assert_not_called()

    def test_missing_engine_metadata_is_distinct_from_a_proven_mismatch(self):
        self.declared = {}
        with self.assertRaisesRegex(
            devices.DeviceError, "verifiable saved device metadata"
        ):
            self.verify()
        info = {
            "host": {"serviceIsRemote": True, "security": {"rootless": True}},
            "store": {"graphRoot": str(GRAPH), "graphDriverName": "overlay"},
        }
        with (
            patch.object(
                box,
                "podman",
                return_value=SimpleNamespace(returncode=0, stdout=json.dumps(info)),
            ) as podman,
            patch.object(devices, "_node", return_value=(1, 2, 3)),
        ):
            with self.assertRaisesRegex(devices.DeviceError, "local rootless"):
                devices.verify(box, self.data, self.info, ENGINE)
        podman.assert_called_once()

    def test_other_mount_type_cannot_shadow_a_saved_node(self):
        self.oci["mounts"].append(
            {"type": "tmpfs", "source": "tmpfs", "destination": NODES[0], "options": []}
        )
        with self.assertRaisesRegex(devices.DeviceError, "do not match"):
            self.verify()

    def ephemeral_binds(self):
        self.info["State"] = {
            "Status": "exited",
            "Running": False,
            "Paused": False,
            "Restarting": False,
            "Pid": 0,
            "ConmonPid": 0,
        }
        sources = {
            "/etc/hostname": "hostname",
            "/etc/hosts": "hosts",
            "/etc/resolv.conf": "resolv.conf",
            "/run/.containerenv": ".containerenv",
        }
        mounts = [
            {"type": "bind", "source": str(RUNDIR / name), "destination": target}
            for target, name in sources.items()
        ]
        self.oci["mounts"].extend(mounts)
        return mounts

    @staticmethod
    def private_runtime_directory(_path):
        return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=os.geteuid())

    def test_stopped_exact_podman_runtime_bind_files_may_be_absent(self):
        self.ephemeral_binds()

        def metadata(path):
            if path.parent == RUNDIR:
                raise FileNotFoundError
            return self.private_runtime_directory(path)

        with (
            patch.object(devices.os, "stat", side_effect=FileNotFoundError),
            patch.object(Path, "lstat", autospec=True, side_effect=metadata),
        ):
            self.assertEqual(len(self.verify()), len(NODES))

    def test_stopped_runtime_subdirectory_may_already_be_cleaned_up(self):
        self.ephemeral_binds()

        def metadata(path):
            if path == RUNDIR:
                raise FileNotFoundError
            return self.private_runtime_directory(path)

        with (
            patch.object(devices.os, "stat", side_effect=FileNotFoundError),
            patch.object(Path, "lstat", autospec=True, side_effect=metadata),
        ):
            self.assertEqual(len(self.verify()), len(NODES))

    def test_missing_bind_from_other_source_or_running_state_refuses(self):
        mounts = self.ephemeral_binds()
        with (
            patch.object(devices.os, "stat", side_effect=FileNotFoundError),
            patch.object(Path, "lstat", autospec=True, side_effect=FileNotFoundError),
        ):
            running = {
                **self.info["State"],
                "Running": True,
                "Status": "running",
                "Pid": 7,
            }
            saved = self.info["State"]
            self.info["State"] = running
            with self.assertRaisesRegex(devices.DeviceError, "non-device bind source"):
                self.verify()
            self.info["State"] = saved
            mounts[0]["source"] = str(
                RUNDIR.parent.parent / ("b" * 64) / "userdata/hostname"
            )
            with self.assertRaisesRegex(devices.DeviceError, "non-device bind source"):
                self.verify()

    def test_wrong_runtime_type_or_dangling_symlink_is_not_a_missing_engine_file(self):
        mounts = self.ephemeral_binds()
        for unsafe_path, mode in (
            (RUNDIR, stat.S_IFLNK | 0o777),
            (RUNDIR / "hostname", stat.S_IFLNK | 0o777),
            (RUN, stat.S_IFDIR | 0o777),
        ):
            with self.subTest(path=unsafe_path):

                def metadata(path):
                    if path == unsafe_path:
                        return SimpleNamespace(st_mode=mode, st_uid=os.geteuid())
                    return self.private_runtime_directory(path)

                with patch.object(Path, "lstat", autospec=True, side_effect=metadata):
                    self.assertFalse(
                        devices._missing_runtime_file(
                            RUN,
                            IDENT,
                            self.info,
                            mounts[0]["source"],
                            mounts[0]["destination"],
                        )
                    )

    def test_missing_node_explains_how_to_recover_without_clearing_it(self):
        with patch.object(devices, "_node", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(devices.DeviceError, "Reconnect or enable"):
                devices.verify(box, self.data, self.info, ENGINE)

    def test_scoped_create_override_preserves_no_device_path_and_retains_engine(self):
        with devices.creation_env(ENGINE, ()) as passed:
            self.assertIs(passed, ENGINE)
        with devices.creation_env(ENGINE, NODES) as passed:
            path = Path(passed.pop("CONTAINERS_CONF_OVERRIDE"))
            self.assertEqual(
                path.read_text(), "[containers]\ndevices = [{append = false}]\n"
            )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(passed, ENGINE)
        self.assertFalse(path.exists())
        with self.assertRaisesRegex(devices.DeviceError, "ambient"):
            with devices.creation_env(
                {**ENGINE, "CONTAINERS_CONF_OVERRIDE": "/already-set"}, NODES
            ):
                pass
