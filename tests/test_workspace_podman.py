"""Synthetic evidence only: no Podman process or real host profile is used."""

from contextlib import ExitStack, contextmanager
from copy import deepcopy
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from oc_box import workspace_podman as podman
from oc_box import workspace_uninstall as review


_UID = 2468
_ENV = {
    "HOME": "/synthetic/home",
    "XDG_RUNTIME_DIR": f"/run/user/{_UID}",
    "OPENAI_API_KEY": "PRIVATE_VALUE",
    "LD_PRELOAD": "PRIVATE_HOOK",
    "PATH": "/untrusted/user-path",
}
_CONFIG = _ENV["HOME"] + "/.config"
_GRAPH = _ENV["HOME"] + "/.local/share/containers/storage"
_RUN = f"/run/user/{_UID}/containers"
_VOLUMES = _GRAPH + "/volumes"
_BINARY = "/synthetic/bin/podman"
_ID = "a" * 32
_CONTAINER = "oc-box-" + _ID
_HOME = "oc-box-home-" + _ID
_CID = "9" * 64
_UPPER = _GRAPH + "/overlay/" + "8" * 64 + "/diff"
_CREATED = "2026-02-01T04:05:06.878654321Z"
_EXPECTED = ((_CONTAINER, _HOME),)
_INFO = ("info", "--format", "json")
_PS = ("ps", "--all", "--quiet", "--no-trunc")
_MOUNT_LIST = ("mount", "--format", "json")
_VLS = ("volume", "ls", "--format", "{{.Name}}")
_NLS = (
    "network",
    "ls",
    "--filter",
    "label=io.oc-box.workspace",
    "--format",
    "{{.Name}}",
)


class FixturePin:
    def __init__(self, path, *, owner, directory):
        self.path = path
        self.fd = 99
        self.recheck = Mock()
        mount = 6 if path == _RUN else 5
        inode = sum(path.encode("ascii"))
        mode = 0o40700 if directory else 0o100755
        self.stamp = (4, inode, mount, owner, mode, 1, 0, 0, 0)


class FixtureEngine:
    def __init__(self):
        self.commands = []
        self.pins = {}
        self.info = {
            "host": {"security": {"rootless": True}, "serviceIsRemote": False},
            "store": {
                "graphRoot": _GRAPH,
                "runRoot": _RUN,
                "volumePath": _VOLUMES,
                "graphDriverName": "overlay",
                "transientStore": False,
                "configFile": "/usr/share/containers/storage.conf",
                "graphOptions": {},
            },
            "version": {"Version": "5.8.1"},
            "plugins": {"volume": ["local"]},
        }
        self.container = {
            "Id": _CID,
            "Name": _CONTAINER,
            "Created": _CREATED,
            "Rootfs": "",
            "Pod": "",
            "Namespace": "",
            "IsInfra": False,
            "IsService": False,
            "Driver": "overlay",
            "GraphDriver": {"Name": "overlay", "Data": {"UpperDir": _UPPER}},
            "Dependencies": [],
            "HostConfig": {"RestartPolicy": {"Name": "no"}},
            "Config": {
                "Labels": {
                    "io.oc-box.workspace": _ID,
                    "io.oc-box.managed": "true",
                    "unknown.label": "PRIVATE_LABEL",
                },
                "Env": ["KEY=PRIVATE_CONTAINER_ENV"],
            },
            "State": {
                "Status": "exited",
                "Running": False,
                "Paused": False,
                "Restarting": False,
                "Dead": False,
                "Pid": 0,
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": "/private/project/PRIVATE_BIND_PATH",
                    "Destination": "/workspace",
                    "Driver": "",
                }
            ],
        }
        self.volume = {
            "Driver": "local",
            "Scope": "local",
            "Name": _HOME,
            "Labels": {"io.oc-box.workspace": _ID},
            "Mountpoint": _VOLUMES + "/" + _HOME + "/_data",
            "Options": {},
            "MountCount": 0,
            "CreatedAt": _CREATED,
        }
        self.contains_container = self.contains_volume = self.external = False
        self.networks = b"retained-net\n"
        self.extra = ()
        self.count_override = None
        self.mounted = None
        self.bad_volume_mount = False
        self.override = {}

    def pin(self, stack, path, uid, *, owner, directory):
        self.pins[path] = FixturePin(path, owner=owner, directory=directory)
        if self.bad_volume_mount and path.endswith("/_data"):
            pin = self.pins[path]
            pin.stamp = (*pin.stamp[:2], 88, *pin.stamp[3:])
        return self.pins[path]

    def capture(self, binary, argv, environment, deadline, limit):
        assert binary is self.pins[_BINARY]
        assert argv[0] == "--remote=false"
        assert environment["HOME"] == _ENV["HOME"]
        assert environment["XDG_RUNTIME_DIR"] == f"/run/user/{_UID}"
        assert environment["PATH"] == "/usr/sbin:/usr/bin:/sbin:/bin"
        assert not any(key in environment for key in ("OPENAI_API_KEY", "LD_PRELOAD"))
        assert 0 < deadline - time.monotonic() <= podman._COMMAND_SECONDS
        assert limit in (podman._SMALL, podman._INSPECT)
        cmd = argv[1:]
        self.commands.append(cmd)
        if cmd in self.override:
            value = self.override[cmd]
            return value() if callable(value) else value
        if cmd == _INFO:
            payload = deepcopy(self.info)
            count = self.count_override
            if count is None:
                count = int(self.contains_container) + len(self.extra)
            payload["store"]["containerStore"] = {
                "number": count,
                "running": 0,
                "paused": 0,
                "stopped": count,
            }
            return 0, json.dumps(payload).encode()
        containers = ((self.container,) if self.contains_container else ()) + self.extra
        if cmd == _PS or cmd == (*_PS, "--external"):
            # Keep the original listed ID available for mismatched inspect tests.
            ids = ((_CID,) if self.contains_container else ()) + tuple(
                row["Id"] for row in self.extra
            )
            ids = b"".join(ident.encode() + b"\n" for ident in ids)
            if cmd[-1] == "--external" and self.external:
                ids += b"7" * 64 + b"\n"
            return 0, ids
        if cmd == _VLS:
            return 0, _HOME.encode() + b"\n" if self.contains_volume else b""
        if cmd == _NLS:
            return 0, self.networks
        if cmd == _MOUNT_LIST:
            return (
                (125, b"PRIVATE_NATIVE_OVERLAY_REFUSAL")
                if self.mounted is None
                else (0, json.dumps(self.mounted).encode())
            )
        if cmd[:3] == ("container", "inspect", "--"):
            return 0, json.dumps(containers).encode()
        if cmd == ("volume", "inspect", "--", _HOME) and self.contains_volume:
            return 0, json.dumps([self.volume]).encode()
        if cmd == ("container", "exists", "--", _CONTAINER):
            return (0 if self.contains_container else 1), b""
        if cmd == ("volume", "exists", "--", _HOME):
            return (0 if self.contains_volume else 1), b""
        raise AssertionError("Unexpected synthetic engine command")

    @contextmanager
    def local(self, *, environment=None, executable=_BINARY):
        with (
            patch.object(podman, "_host_uid", return_value=_UID),
            patch.object(podman, "_no_overrides") as overrides,
            patch.object(podman, "_vendor_pin", return_value=None),
            patch.object(podman, "_vendor_unchanged"),
            patch.object(podman, "_pin", side_effect=self.pin),
            patch.object(podman, "_read_pin", return_value=b"\x7fELF"),
            podman.local_inventory(
                executable,
                environment=_ENV if environment is None else environment,
                capture=self.capture,
            ) as reader,
        ):
            yield reader, overrides


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = FixtureEngine()

    def refused(self, reason, action, *args):
        with self.assertRaises(podman.InventoryError) as caught:
            action(*args)
        self.assertEqual(caught.exception.reason, reason)
        self.assertNotIn("PRIVATE_", str(caught.exception))

    def test_pinned_empty_profile_and_double_catalog_with_expected_absence(self):
        with self.engine.local() as (reader, overrides):
            initial = reader.profile
            result = reader.inspect(_EXPECTED)
            self.assertTrue(result.complete)
            self.assertEqual(result.scope, initial.scope)
            self.assertEqual(result.labeled_networks, ("retained-net",))
            self.assertEqual(
                self.engine.commands,
                [
                    _INFO,
                    _INFO,
                    _PS,
                    (*_PS, "--external"),
                    _VLS,
                    _NLS,
                    ("container", "exists", "--", _CONTAINER),
                    ("volume", "exists", "--", _HOME),
                    _PS,
                    (*_PS, "--external"),
                    _VLS,
                    _NLS,
                    _INFO,
                ],
            )
            for path in (
                _BINARY,
                _ENV["HOME"],
                f"/run/user/{_UID}",
                _GRAPH,
                _RUN,
                _VOLUMES,
            ):
                self.assertEqual(
                    self.engine.pins[path].recheck.call_count,
                    2 * len(self.engine.commands),
                )
            self.assertEqual(overrides.call_count, 1 + 2 * len(self.engine.commands))
        self.refused("engine-context-closed", reader.scan, _EXPECTED)

    def test_populated_inventory_stays_incomplete_and_has_no_raw_mount_or_env_data(
        self,
    ):
        self.engine.contains_container = self.engine.contains_volume = True
        self.engine.volume["MountCount"] = 2
        with self.engine.local() as (reader, _):
            observed = reader.scan(_EXPECTED)
            self.assertFalse(observed.complete)
            (container,) = observed.containers
            (volume,) = observed.volumes
            self.assertEqual(
                (container.ident, container.status, container.mount_entries),
                (_CID, "exited", 1),
            )
            self.assertEqual((volume.name, volume.mount_count), (_HOME, 2))
            self.assertEqual(volume.mountpoint.path, self.engine.volume["Mountpoint"])
            self.assertNotIn("PRIVATE_", repr(observed))
            self.assertIsNone(container.mounted)
            self.assertEqual(self.engine.commands.count(_MOUNT_LIST), 1)
            self.assertEqual(
                (container.conmon_pid, container.writable.path), (0, _UPPER)
            )
            self.assertNotIn("/private/project/PRIVATE_BIND_PATH", self.engine.pins)
            self.refused("populated-store-proof-unavailable", reader.inspect, _EXPECTED)

    def test_captures_stopped_named_volume_consumers_with_zero_mount_count(self):
        self.engine.contains_container = self.engine.contains_volume = True
        foreign = deepcopy(self.engine.container)
        foreign.update(
            Id="7" * 64,
            Name="foreign-stopped",
            Dependencies=[_CID],
            Mounts=[
                {
                    "Type": "volume",
                    "Driver": "local",
                    "Name": _HOME,
                    "Source": self.engine.volume["Mountpoint"],
                    "Destination": "/data",
                    "SubPath": "nested",
                },
                {
                    "Type": "bind",
                    "Driver": "",
                    "Source": self.engine.volume["Mountpoint"] + "/child",
                    "Destination": "/alias",
                },
            ],
        )
        foreign["GraphDriver"]["Data"]["UpperDir"] = (
            _GRAPH + "/overlay/" + "6" * 64 + "/diff"
        )
        foreign["Config"]["Labels"] = None
        self.engine.extra = (foreign,)
        self.engine.mounted = []
        with self.engine.local() as (reader, _):
            found = reader.scan(_EXPECTED)
            old, owner = found.containers
            self.assertEqual(old.dependencies, (owner.ident,))
            self.assertEqual(old.volume_refs, ((_HOME, True),))
            self.assertEqual(old.bind_entries, 1)
            self.assertTrue(old.lexical_store_overlap)
            self.assertFalse(owner.lexical_store_overlap)
            self.assertEqual(owner.bind_entries, 1)
            self.assertFalse(old.mounted)
            self.assertEqual(found.volumes[0].mount_count, 0)
            self.assertFalse(found.complete)
            self.assertNotIn(foreign["Mounts"][1]["Source"], self.engine.pins)

    def test_only_exact_operand_free_mount_report_can_prove_mounted_status(self):
        self.engine.contains_container = True
        row = {
            "id": _CID,
            "Names": [_CONTAINER],
            "mountpoint": _UPPER.removesuffix("/diff") + "/merged",
        }
        self.engine.mounted = [row]
        with self.engine.local() as (reader, _):
            found = reader.scan(_EXPECTED)
            self.assertTrue(found.containers[0].mounted)
            self.assertEqual(self.engine.commands.count(_MOUNT_LIST), 2)
            self.assertNotIn(row["mountpoint"], self.engine.pins)
            for bad in (
                [dict(row, id="f" * 64)],
                [dict(row, Names=["replacement"])],
                [dict(row, mountpoint="/PRIVATE_EXTERNAL")],
                [{"id": _CID, "names": [_CONTAINER], "mountpoint": row["mountpoint"]}],
            ):
                self.engine.mounted = bad
                self.refused("mount-catalog-unverified", reader.scan, _EXPECTED)
            results = iter(
                ((0, json.dumps([row]).encode()), (125, b"PRIVATE_UNAVAILABLE"))
            )
            self.engine.override[_MOUNT_LIST] = lambda: next(results)
            self.refused("catalog-changed", reader.scan, _EXPECTED)

    def test_invalid_missing_dependency_external_storage_and_incomplete_catalog_refuse(
        self,
    ):
        self.engine.contains_container = True
        normal = deepcopy(self.engine.container)
        with self.engine.local() as (reader, _):
            self.engine.count_override = 2
            self.refused("catalog-changed", reader.scan, _EXPECTED)
            self.engine.count_override = None
            for evidence in (None, [_CID], ["7" * 64], ["prefix"]):
                self.engine.container["Dependencies"] = evidence
                self.refused("inspect-unverified", reader.scan, _EXPECTED)
            self.engine.container = deepcopy(normal)
            self.engine.container["GraphDriver"]["Data"]["UpperDir"] = (
                "/PRIVATE_EXTERNAL"
            )
            self.refused("unsupported-container-storage", reader.scan, _EXPECTED)
            self.engine.container = deepcopy(normal)
            self.engine.container["Mounts"][0]["Type"] = "image"
            self.refused("unsupported-container-storage", reader.scan, _EXPECTED)
            self.engine.container = deepcopy(normal)
            volume = {
                "Type": "volume",
                "Name": _HOME,
                "Driver": "local",
                "Source": self.engine.volume["Mountpoint"],
                "Destination": "/data",
            }
            self.engine.container["Mounts"].append(volume)
            self.refused("inspect-unverified", reader.scan, _EXPECTED)
            volume["SubPath"] = "../outside"
            self.refused("unsupported-container-storage", reader.scan, _EXPECTED)

    def test_same_names_with_changed_creation_or_refcount_do_not_pass_second_inspect(
        self,
    ):
        self.engine.contains_container = self.engine.contains_volume = True
        initial = self.engine.volume
        with self.engine.local() as (reader, _):
            for changed in (
                dict(initial, CreatedAt="2026-04-04T01:01:01Z"),
                dict(initial, MountCount=1),
            ):
                results = iter(
                    (
                        (0, json.dumps([initial]).encode()),
                        (0, json.dumps([changed]).encode()),
                    )
                )
                self.engine.override[("volume", "inspect", "--", _HOME)] = lambda: next(
                    results
                )
                self.refused("catalog-changed", reader.scan, _EXPECTED)

    def test_remote_and_ambiguous_environment_never_reach_engine(self):
        for key, value in (
            ("CONTAINER_HOST", "PRIVATE_HOST"),
            ("CONTAINER_CONNECTION", ""),
            ("CONTAINERS_CONF_OVERRIDE", "PRIVATE_FILE"),
            ("PODMAN_CONNECTIONS_CONF", ""),
            ("STORAGE_DRIVER", "overlay"),
            ("DOCKER_CONTEXT", "default"),
            ("XDG_RUNTIME_DIR", "/another/run"),
            ("XDG_CONFIG_HOME", "/another/config"),
        ):
            with self.subTest(key=key):
                with self.assertRaisesRegex(
                    podman.InventoryError, "unsupported-engine-selector"
                ):
                    with self.engine.local(environment={**_ENV, key: value}):
                        self.fail("unexpected engine context")
        for filename in (
            "podman",
            "/synthetic/bin/podman-remote",
            "/synthetic/bin/../podman",
        ):
            with self.assertRaisesRegex(
                podman.InventoryError, "engine-executable-unverified"
            ):
                with self.engine.local(executable=filename):
                    self.fail("unexpected engine context")
        with self.assertRaisesRegex(
            podman.InventoryError, "unsupported-engine-selector"
        ):
            with self.engine.local(environment={"HOME": _ENV["HOME"]}):
                self.fail("unexpected engine context without runtime selector")
        self.assertEqual(self.engine.commands, [])

    def test_native_scope_fields_exact_and_binding_must_stay_same(self):
        initial = deepcopy(self.engine.info)
        for section, key, value in (
            ("host", "serviceIsRemote", True),
            ("host", "security", {"rootless": 1}),
            ("store", "graphDriverName", "vfs"),
            ("store", "graphRoot", "/elsewhere/storage"),
            ("store", "transientStore", None),
            ("store", "configFile", "/elsewhere/storage.conf"),
            ("version", "Version", "5.8.1-dev"),
            ("version", "Version", "6.2.1"),
            ("plugins", "volume", ["local", "PRIVATE_PLUGIN"]),
        ):
            with self.subTest(section=section, key=key):
                self.engine.info = deepcopy(initial)
                self.engine.info[section][key] = value
                with self.assertRaisesRegex(
                    podman.InventoryError, "engine-profile-unverified"
                ):
                    with self.engine.local():
                        self.fail("unexpected engine context")
        self.engine.info = initial
        self.assertNotIn(_VLS, self.engine.commands)
        with self.engine.local() as (reader, _):
            self.engine.info["version"]["Version"] = "5.8.2"
            self.refused("engine-profile-unverified", reader.scan, _EXPECTED)

    def test_store_pin_change_rejects_before_query_and_after_capture(self):
        with self.engine.local() as (reader, _):
            pin = self.engine.pins[_GRAPH]
            pin.recheck.side_effect = podman.InventoryError("engine-binding-changed")
            self.refused("engine-binding-changed", reader.scan, _EXPECTED)
            self.assertEqual(self.engine.commands, [_INFO])
            pin.recheck.side_effect = None

            def replacement():
                pin.recheck.side_effect = podman.InventoryError(
                    "engine-binding-changed"
                )
                return 0, b""

            self.engine.override[_PS] = replacement
            self.refused("engine-binding-changed", reader.scan, _EXPECTED)
            self.assertEqual(self.engine.commands, [_INFO, _INFO, _PS])

    def test_external_objects_and_catalog_or_exists_disagreement_refuse(self):
        with self.engine.local() as (reader, _):
            self.engine.external = True
            self.refused("external-container-store", reader.scan, _EXPECTED)
            self.engine.external = False
            self.engine.override[("volume", "exists", "--", _HOME)] = (0, b"")
            self.refused("catalog-changed", reader.scan, _EXPECTED)
            self.engine.override.clear()
            reads = iter(((0, b"retained-net\n"), (0, b"another-net\n")))
            self.engine.override[_NLS] = lambda: next(reads)
            self.refused("catalog-changed", reader.scan, _EXPECTED)

    def test_output_limits_catalog_duplicates_and_engine_diagnostics(self):
        with self.engine.local() as (reader, _):
            for raw, reason in (
                (b"x" * (podman._SMALL + 1), "engine-output-limit"),
                ((_CID.encode() + b"\n") * 2, "catalog-unverified"),
                (_CID.encode() + b"\r\n", "catalog-unverified"),
                (_CID.encode(), "catalog-unverified"),
            ):
                self.engine.override[_PS] = (0, raw)
                self.refused(reason, reader.scan, _EXPECTED)
            self.engine.override[_PS] = (125, b"PRIVATE_NATIVE_ENGINE_ERROR")
            self.refused("engine-command-unverified", reader.scan, _EXPECTED)
            self.engine.override[_PS] = (True, b"")
            self.refused("engine-command-unverified", reader.scan, _EXPECTED)

    def test_volume_external_mapping_missing_schema_and_incorrect_mount_id(self):
        self.engine.contains_volume = True
        initial = deepcopy(self.engine.volume)
        with self.engine.local() as (reader, _):
            for key, value in (
                ("Scope", "global"),
                ("Mountpoint", "/PRIVATE_EXTERNAL"),
                ("Options", {"device": "/PRIVATE_EXTERNAL"}),
                ("MountCount", False),
                ("StorageID", "external"),
                ("Anonymous", True),
                ("CreatedAt", "0001-01-01T00:00:00Z"),
            ):
                with self.subTest(key=key):
                    self.engine.volume = {**initial, key: value}
                    self.refused("volume-proof-unavailable", reader.scan, _EXPECTED)
            self.engine.volume = {
                key: value for key, value in initial.items() if key != "MountCount"
            }
            self.refused("volume-proof-unavailable", reader.scan, _EXPECTED)
            self.engine.volume = initial
            self.engine.bad_volume_mount = True
            self.refused("volume-proof-unavailable", reader.scan, _EXPECTED)

    def test_unknown_label_wrong_inspected_id_missing_state_and_duplicate_json_refuse(
        self,
    ):
        self.engine.contains_container = True
        original = deepcopy(self.engine.container)
        with self.engine.local() as (reader, _):
            self.engine.container["Config"]["Labels"]["io.oc-box.workspace"] = (
                "PRIVATE_LABEL"
            )
            self.refused("inspect-unverified", reader.scan, _EXPECTED)
            self.engine.container = deepcopy(original)
            self.engine.container["Id"] = "a" * 64
            self.refused("inspect-unverified", reader.scan, _EXPECTED)
            self.engine.container = deepcopy(original)
            del self.engine.container["State"]["Pid"]
            self.refused("inspect-unverified", reader.scan, _EXPECTED)
            self.engine.override[("container", "inspect", "--", _CID)] = (
                0,
                b'[{"Id": "PRIVATE_ONE", "Id": "PRIVATE_TWO"}]',
            )
            self.refused("malformed-engine-metadata", reader.scan, _EXPECTED)

    def test_concurrent_or_reentrant_scan_and_bad_expected_shapes_refuse(self):
        with self.engine.local() as (reader, _):
            before = len(self.engine.commands)
            for names in (
                (_EXPECTED[0], _EXPECTED[0]),
                (("--all", _HOME),),
                ((_CONTAINER, "oc-box-home-" + "b" * 32),),
            ):
                self.refused("invalid-expected-resources", reader.scan, names)
            error = []

            def other_thread():
                try:
                    reader.scan(_EXPECTED)
                except podman.InventoryError as exc:
                    error.append(exc.reason)

            worker = threading.Thread(target=other_thread)
            worker.start()
            worker.join(timeout=2)
            self.assertEqual(error, ["engine-inventory-busy"])
            self.assertEqual(len(self.engine.commands), before)

            def reentrant():
                self.refused("engine-inventory-busy", reader.scan, _EXPECTED)
                return 0, b""

            self.engine.override[_PS] = reentrant
            self.assertTrue(reader.scan(_EXPECTED).complete)

    def test_deadline_exhaustion_stops_without_another_command(self):
        clock = [100]

        def delayed():
            clock[0] += 60
            return 0, b""

        with patch.object(time, "monotonic", side_effect=lambda: clock[0]):
            with self.engine.local() as (reader, _):
                self.engine.override[_PS] = delayed
                self.refused("engine-timeout", reader.scan, _EXPECTED)
        self.assertEqual(self.engine.commands, [_INFO, _INFO, _PS])

    def test_planner_cannot_treat_incomplete_inventory_as_absence(self):
        self.engine.contains_volume = True
        with tempfile.TemporaryDirectory() as directory:
            absent = Path(directory) / "registry"
            with self.engine.local() as (reader, _):
                with self.assertRaisesRegex(
                    review.WorkspacePlanError, "engine-inventory-unverified"
                ):
                    review.plan_workspaces(
                        absent, inspect_engine=reader.inspect, protected_paths=()
                    )
            self.assertFalse(absent.exists())


class NativeSeamTests(unittest.TestCase):
    def test_site_override_is_detected_before_any_content_read(self):
        entered = []

        @contextmanager
        def fake_path(path, uid):
            entered.append(path)
            if path != _CONFIG + "/containers/podman-connections.json":
                raise FileNotFoundError
            yield 3, None

        with patch.object(podman, "_path_fd", fake_path):
            with self.assertRaisesRegex(
                podman.InventoryError, "unsupported-engine-config"
            ):
                podman._no_overrides(_CONFIG, _UID)
        self.assertIn("/etc/containers/containers.rootless.conf", entered)

    def test_vendor_remote_and_defaults_are_checked_with_pinned_toml(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest("TOML parser intentionally not provided on Python 3.10")
        pin = FixturePin("/vendor", owner=0, directory=False)
        with patch.object(podman, "_pin", return_value=pin), ExitStack() as stack:
            for body in (
                b"[engine]\nremote = true\n",
                b'[engine]\nactive_service = "PRIVATE_REMOTE"\n',
                b'[engine]\nstatic_dir = "/PRIVATE_STORE"\n',
                b'[engine.volume_plugins]\nexternal = "/PRIVATE_PLUGIN_SOCKET"\n',
                b'[engine.service_destinations.named]\nuri = "PRIVATE_REMOTE"\n',
            ):
                with patch.object(podman, "_read_pin", return_value=body):
                    with self.assertRaisesRegex(
                        podman.InventoryError, "unsupported-engine-config"
                    ):
                        podman._vendor_pin(stack, _UID)
            with patch.object(
                podman, "_read_pin", return_value=b"[engine]\nremote = false\n"
            ):
                self.assertIs(podman._vendor_pin(stack, _UID), pin)
        pin.recheck.assert_called_once()

    def test_descriptor_binding_refuses_synthetic_link_and_inode_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            binary = home / "podman"
            binary.write_bytes(b"\x7fELF")
            uid = os.geteuid()
            with (
                patch.object(review, "_mount_id", return_value=27),
                ExitStack() as stack,
            ):
                pin = podman._pin(stack, str(binary), uid, owner=uid, directory=False)
                pin.recheck()
                binary.rename(home / "old")
                binary.write_bytes(b"\x7fELF")
                with self.assertRaisesRegex(
                    podman.InventoryError, "engine-binding-changed"
                ):
                    pin.recheck()
                link = home / "substitute"
                link.symlink_to(binary)
                with self.assertRaisesRegex(podman.InventoryError, "path-unverified"):
                    podman._pin(stack, str(link), uid, owner=uid, directory=False)

    def test_bounded_private_capture_passes_pinned_fd_and_reaps_only_direct_child(self):
        binary = FixturePin(_BINARY, owner=0, directory=False)
        info = ("--remote=false", *_INFO)

        def fake_process(raw, pending):
            read, write = os.pipe()
            os.write(write, raw)
            os.close(write)
            fake = Mock(stdout=os.fdopen(read, "rb"))
            fake.poll.return_value = None if pending else 0
            fake.wait.return_value = 0
            return fake

        good = fake_process(b"{}\n", False)
        with patch.object(subprocess, "Popen", return_value=good) as spawn:
            self.assertEqual(
                podman._private_capture(
                    binary,
                    info,
                    {"LANG": "C"},
                    time.monotonic() + 2,
                    3,
                ),
                (0, b"{}\n"),
            )
        self.assertEqual(
            spawn.call_args.kwargs["executable"], f"/proc/self/fd/{binary.fd}"
        )
        self.assertEqual(spawn.call_args.kwargs["pass_fds"], (binary.fd,))
        self.assertEqual(spawn.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(spawn.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(good.stdout.closed)
        good.kill.assert_not_called()
        large = fake_process(b"PRIVATE_OUTPUT", True)
        with patch.object(subprocess, "Popen", return_value=large):
            with self.assertRaisesRegex(podman.InventoryError, "engine-output-limit"):
                podman._private_capture(binary, info, {}, time.monotonic() + 2, 2)
        large.kill.assert_called_once()
        large.wait.assert_called_once_with(timeout=podman._REAP_SECONDS)
        self.assertTrue(large.stdout.closed)

        late = fake_process(b"", True)
        late.wait.side_effect = (
            subprocess.TimeoutExpired(["PRIVATE_COMMAND"], 0),
            0,
        )
        with patch.object(subprocess, "Popen", return_value=late):
            with self.assertRaisesRegex(podman.InventoryError, "engine-timeout"):
                podman._private_capture(binary, info, {}, time.monotonic() + 2, 2)
        late.kill.assert_called_once()
        self.assertEqual(late.wait.call_count, 2)
        self.assertTrue(late.stdout.closed)

    def test_read_only_transport_refuses_mutation_and_unknown_flags_without_spawn(self):
        binary = FixturePin(_BINARY, owner=0, directory=False)
        with patch.object(subprocess, "Popen") as spawn:
            for args in (
                ("--remote=false", "container", "rm", "--", _CID),
                ("--remote=false", "system", "prune"),
                ("--remote=false", "mount", "--all", "--format", "json"),
                ("--remote=false", "mount", "--latest"),
                ("--remote=false", "mount", _CID),
                ("--remote=false", "container", "inspect", "--", "--all"),
                ("--remote=false", "container", "inspect", "--", "short-id"),
                ("--remote=true", *_INFO),
            ):
                with self.assertRaisesRegex(
                    podman.InventoryError, "engine-command-unverified"
                ):
                    podman._private_capture(binary, args, {}, time.monotonic() + 2, 32)
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
