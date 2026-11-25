import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from oc_box import guarded_devices as devices, oc_box as box


NODES = ["/dev/accel/accel0", "/dev/dri/renderD128"]
IDENT = "a" * 64
ENGINE = {"PATH": "/usr/bin", "CONTAINERS_CONF": "/synthetic/local.conf"}
GRAPH = Path("/synthetic/containers")
BUNDLE = GRAPH / "overlay-containers" / IDENT / "userdata"


class GuardedDevicesTests(unittest.TestCase):
    def setUp(self):
        self.data = {"devices": NODES.copy()}
        self.info = {
            "Id": IDENT,
            "HostConfig": {"Privileged": False, "Devices": []},
            "Config": {
                "CreateCommand": ["podman", "create", *(f"--device={d}" for d in NODES)]
            },
            "OCIConfigPath": str(BUNDLE / "config.json"),
        }
        self.declared = {
            "host_device_list": [
                {"path": path, "type": "", "major": 0, "minor": 0} for path in NODES
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
            "store": {"graphRoot": str(GRAPH), "graphDriverName": "overlay"},
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
        self.assertEqual(stat.call_count, 4)
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
        self.assertEqual(len(self.verify(fresh=True)), 2)

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

    def test_missing_node_explains_how_to_recover_without_clearing_it(self):
        with patch.object(devices, "_node", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(devices.DeviceError, "Reconnect or enable"):
                devices.verify(box, self.data, self.info, ENGINE)

    def test_scoped_create_override_preserves_no_device_path_and_retains_engine(self):
        with devices.creation_env(ENGINE, ()) as passed:
            self.assertIs(passed, ENGINE)
        with devices.creation_env(ENGINE, NODES) as passed:
            path = Path(passed.pop("CONTAINERS_CONF_OVERRIDE"))
            self.assertEqual(path.read_text(), "[containers]\ndevices = []\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(passed, ENGINE)
        self.assertFalse(path.exists())
        with self.assertRaisesRegex(devices.DeviceError, "ambient"):
            with devices.creation_env(
                {**ENGINE, "CONTAINERS_CONF_OVERRIDE": "/already-set"}, NODES
            ):
                pass
