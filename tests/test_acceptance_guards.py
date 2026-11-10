"""Synthetic, offline shared-guard contracts; no engine or host acceptance."""

import copy
from dataclasses import FrozenInstanceError, asdict, replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import acceptance_guards as guards


class AcceptanceGuardsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="guard-unit-")
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "case"
        self.root.mkdir(mode=0o700)
        self.uid = os.getuid()

    def file(self, relative, data=b"{}", mode=0o600):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def refusal(self, code, function, *args, **kwargs):
        with self.assertRaises(guards.GuardError) as result:
            function(*args, **kwargs)
        self.assertEqual(result.exception.code, code)
        self.assertEqual(str(result.exception), code)
        self.assertEqual(result.exception.args, (code,))

    def read_json(self, relative="record.json", **kwargs):
        return guards.read_private_json(
            self.root,
            relative,
            uid=kwargs.pop("uid", self.uid),
            max_bytes=kwargs.pop("max_bytes", 128),
            **kwargs,
        )

    def test_closed_errors_and_immutable_records(self):
        self.assertEqual(str(guards.GuardError("private payload")), "guard-error")
        identity = guards.NativeIdentity(self.root / "native", "0" * 64, 4)
        with self.assertRaises(FrozenInstanceError):
            identity.bytes = 5
        checkpoint = guards.BootCheckpoint(False, False, False, True, None)
        self.assertEqual(
            asdict(checkpoint),
            {
                "cached_marker_present": False,
                "cached_marker_empty": False,
                "cached_id_valid": False,
                "current_id_valid": True,
                "boot_ids_match": None,
            },
        )

    def test_checked_path_shape_and_ancestor_link_matrix(self):
        leaf = self.file("nested/data")
        self.assertEqual(
            guards.checked_path("nested/data", root=self.root, kind="file"), leaf
        )
        self.assertEqual(guards.checked_path(self.root, kind="directory"), self.root)
        missing = self.root / "missing/child"
        self.assertEqual(guards.checked_path(missing, optional=True), missing)
        outside = self.parent / "outside"
        outside.mkdir()
        link = self.root / "redirect"
        link.symlink_to(outside, target_is_directory=True)
        dangling = self.root / "dangling"
        dangling.symlink_to(self.parent / "absent")
        for name, path, kwargs in (
            ("ancestor-link", link / "missing", {"optional": True}),
            ("dangling-leaf", dangling, {"optional": True}),
            ("traversal", self.root / "nested/../data", {"optional": True}),
            ("nul", str(self.root / "bad") + "\0", {}),
            ("newline", str(self.root / "bad") + "\n", {}),
            ("tab", str(self.root / "bad") + "\t", {}),
            ("delete-control", str(self.root / "bad") + "\x7f", {}),
            ("c1-control", str(self.root / "bad") + "\x85", {}),
            ("missing", missing, {}),
            ("wrong-kind", leaf, {"kind": "directory"}),
            ("non-directory-ancestor", leaf / "child", {"optional": True}),
            ("root-not-child", self.root, {"root": self.root}),
            ("escape", self.parent, {"root": self.root}),
            ("unknown-kind", leaf, {"kind": "socket"}),
        ):
            with self.subTest(name=name):
                self.refusal("unsafe-path", guards.checked_path, path, **kwargs)

    def test_private_json_schema_neutral_and_strict_parse_matrix(self):
        for value in (None, True, 3, "text", [1, {}], {"name": [False]}):
            with self.subTest(value=value):
                raw = json.dumps(value).encode()
                self.file("record.json", raw)
                self.assertEqual(self.read_json(max_bytes=len(raw)), value)
        for name, raw in (
            ("bad-utf8", b'"\xff"'),
            ("trailing", b"{}extra"),
            ("nan", b"NaN"),
            ("infinity", b"Infinity"),
            ("duplicate", b'{"a":1,"a":2}'),
            ("empty", b""),
        ):
            with self.subTest(name=name):
                self.file("record.json", raw)
                self.refusal("private-json", self.read_json)

    def test_private_owner_mode_link_size_matrix(self):
        path = self.file("record.json")
        for name, mutation, kwargs in (
            ("owner", lambda: None, {"uid": self.uid + 1}),
            ("group-read", lambda: path.chmod(0o640), {}),
            ("other-write", lambda: path.chmod(0o602), {}),
            ("hardlink", lambda: os.link(path, self.root / "second"), {}),
            ("oversize", lambda: path.write_bytes(b"[] "), {"max_bytes": 2}),
            ("nonpositive-bound", lambda: None, {"max_bytes": 0}),
            ("boolean-bound", lambda: None, {"max_bytes": True}),
        ):
            with self.subTest(name=name):
                path.chmod(0o600)
                path.write_bytes(b"{}")
                (self.root / "second").unlink(missing_ok=True)
                mutation()
                self.refusal("private-file", self.read_json, **kwargs)

    def test_private_nofollow_and_descriptor_race_matrix(self):
        target = self.file("target.json")
        (self.root / "record.json").symlink_to(target)
        self.refusal("unsafe-path", self.read_json)
        (self.root / "record.json").unlink()
        self.file("slot/record.json")
        original_open = os.open
        swapped = False

        def redirected_open(name, flags, *args, **kwargs):
            nonlocal swapped
            if name == "slot" and kwargs.get("dir_fd") is not None and not swapped:
                swapped = True
                (self.root / "slot").rename(self.root / "saved-slot")
                (self.root / "slot").symlink_to(
                    self.root / "saved-slot", target_is_directory=True
                )
            return original_open(name, flags, *args, **kwargs)

        with patch.object(guards.os, "open", side_effect=redirected_open):
            self.refusal("private-file", self.read_json, "slot/record.json")
        self.assertTrue(swapped)
        self.assertEqual((self.root / "saved-slot/record.json").read_bytes(), b"{}")

        record = self.file("record.json")
        swapped = False

        def redirected_leaf(name, flags, *args, **kwargs):
            nonlocal swapped
            if (
                name == "record.json"
                and kwargs.get("dir_fd") is not None
                and not swapped
            ):
                swapped = True
                record.unlink()
                record.symlink_to(target)
            return original_open(name, flags, *args, **kwargs)

        with patch.object(guards.os, "open", side_effect=redirected_leaf):
            self.refusal("private-file", self.read_json)
        self.assertTrue(swapped)
        self.assertEqual(target.read_bytes(), b"{}")

    def test_private_read_bound_and_midread_mutation(self):
        path = self.file("record.json", b'"' + b"a" * 64 + b'"')
        original_read = os.read
        requested = []

        def bounded_read(fd, amount):
            requested.append(amount)
            return original_read(fd, amount)

        with patch.object(guards.os, "read", side_effect=bounded_read):
            self.assertEqual(self.read_json(max_bytes=66), "a" * 64)
        self.assertLessEqual(sum(requested), 68)

        def changing_read(fd, amount):
            result = original_read(fd, amount)
            if result:
                path.write_bytes(b"[]")
            return result

        with patch.object(guards.os, "read", side_effect=changing_read):
            self.refusal("private-file", self.read_json)

    def test_exact_private_files_complete_bytes_matrix(self):
        raw = b"x" * (1024 * 1024) + b"END"
        path = self.file("selector", raw)
        self.file("empty", b"")
        guards.exact_private_files(
            self.root, {"selector": raw, "empty": b""}, uid=self.uid
        )
        for name, payload in (
            ("same-prefix-wrong-tail", raw[:-1] + b"X"),
            ("append", raw + b"\0"),
            ("short", raw[:-1]),
        ):
            with self.subTest(name=name):
                path.write_bytes(payload)
                self.refusal(
                    "exact-files",
                    guards.exact_private_files,
                    self.root,
                    {"selector": raw},
                    uid=self.uid,
                )
        path.write_bytes(raw)
        path.chmod(0o644)
        self.refusal(
            "private-file",
            guards.exact_private_files,
            self.root,
            {"selector": raw},
            uid=self.uid,
        )

    def test_fresh_parent_retained_ancestor_marker_matrix(self):
        parent = self.root / "new/child"
        parent.mkdir(parents=True)
        kwargs = {
            "prefixes": ("retained-",),
            "markers": (
                "fixture.json",
                "edges-result.json",
                "home-run-*-report.json",
            ),
        }
        self.assertEqual(guards.fresh_parent(parent, **kwargs), parent)
        for name in (
            "fixture.json",
            "edges-result.json",
            "home-run-attempt-report.json",
        ):
            with self.subTest(marker=name):
                marker = self.root / name
                marker.symlink_to(self.parent / "absent")
                self.refusal(
                    "fresh-parent-required", guards.fresh_parent, parent, **kwargs
                )
                marker.unlink()
        retained = self.root / "retained-renamed/child"
        retained.mkdir(parents=True)
        self.refusal("fresh-parent-required", guards.fresh_parent, retained, **kwargs)
        self.refusal(
            "unsafe-path",
            guards.fresh_parent,
            parent,
            prefixes=(),
            markers=("../outside",),
        )

    def test_native_elf_hash_replacement_matrix(self):
        raw = b"\x7fELF" + b"synthetic-only" * 100000
        path = self.file("native", raw, 0o700)
        identity = guards.native_identity(path, outside=(self.parent / "excluded",))
        self.assertEqual(
            identity,
            guards.NativeIdentity(path, hashlib.sha256(raw).hexdigest(), len(raw)),
        )
        self.assertEqual(guards.sha256_file(path), identity.sha256)
        guards.check_native(identity)
        self.refusal(
            "native-executable", guards.native_identity, path, outside=(self.root,)
        )
        for name, data, mode in (
            ("shell", b"#!/bin/sh\nexit 0\n", 0o700),
            ("nonexecutable", raw, 0o600),
            ("same-length-changed", raw[:-1] + b"X", 0o700),
            ("append", raw + b"x", 0o700),
        ):
            with self.subTest(name=name):
                path.write_bytes(data)
                path.chmod(mode)
                self.refusal("native-changed", guards.check_native, identity)
        replacement = self.file("replacement", raw + b"changed", 0o700)
        replacement.replace(path)
        self.refusal("native-changed", guards.check_native, identity)
        replacement = self.file("replacement", raw, 0o700)
        path.unlink()
        path.symlink_to(replacement)
        self.refusal("native-changed", guards.check_native, identity)
        self.refusal("file-hash", guards.sha256_file, path)

    def test_native_discovery_entry_only_and_pinned_checks(self):
        target = self.file("native", b"\x7fELFsynthetic", 0o700)
        entry = self.root / "entry"
        entry.symlink_to(target)
        self.refusal("native-executable", guards.native_identity, entry, outside=())
        pinned = guards.native_identity(entry, outside=(), resolve_entry=True)
        self.assertEqual(pinned.path, target)
        self.refusal(
            "native-executable", guards.native_identity, target, outside=str(self.root)
        )
        other = self.file("other", b"\x7fELFother", 0o700)
        entry.unlink()
        entry.symlink_to(other)
        guards.check_native(pinned)  # Discovery entry was pinned, never re-resolved.
        link_parent = self.root / "linked-directory"
        link_parent.symlink_to(self.root, target_is_directory=True)
        self.refusal(
            "native-executable",
            guards.native_identity,
            link_parent / "native",
            outside=(),
            resolve_entry=True,
        )
        self.refusal("native-changed", guards.check_native, replace(pinned, path=entry))

    def test_boot_missing_stale_and_fresh_matrix(self):
        current = b"00000000-0000-0000-0000-000000000001\n"
        marker = self.root / "alive"
        for name, payload, expected, code in (
            ("missing", None, (False, False, False, True, None), "runtime-boot-marker"),
            ("empty", b"", (True, True, False, True, None), "runtime-boot-marker"),
            (
                "malformed",
                b"private malformed text",
                (True, False, False, True, None),
                "runtime-boot-marker",
            ),
            (
                "stale",
                current.replace(b"1", b"2"),
                (True, False, True, True, False),
                "runtime-boot-mismatch",
            ),
            (
                "no-newline-is-distinct",
                current.rstrip(),
                (True, False, True, True, False),
                "runtime-boot-mismatch",
            ),
            ("matching", current, (True, False, True, True, True), None),
        ):
            with self.subTest(name=name):
                marker.unlink(missing_ok=True)
                if payload is not None:
                    self.file(
                        "alive", payload, 0o644
                    )  # Native marker is not necessarily 0600.
                checkpoint = guards.boot_checkpoint(marker, current, uid=self.uid)
                self.assertEqual(tuple(asdict(checkpoint).values()), expected)
                if code:
                    self.refusal(code, guards.require_boot, checkpoint)
                else:
                    guards.require_boot(checkpoint)
                if payload in (None, b""):
                    guards.require_boot(checkpoint, fresh_empty=True)
                elif code:
                    self.refusal(
                        code, guards.require_boot, checkpoint, fresh_empty=True
                    )
                self.assertNotIn(current.decode().strip(), repr(checkpoint))
                self.assertEqual(
                    marker.read_bytes() if marker.exists() else None, payload
                )
        invalid = guards.boot_checkpoint(marker, b"invalid-current", uid=self.uid)
        self.refusal(
            "runtime-boot-current", guards.require_boot, invalid, fresh_empty=True
        )
        marker.unlink()
        missing = guards.boot_checkpoint(marker, current, uid=self.uid)
        self.refusal(
            "runtime-boot-marker", guards.require_boot, missing, fresh_empty="yes"
        )

    def test_boot_metadata_refusal_matrix(self):
        current = b"00000000-0000-0000-0000-000000000001\n"
        marker = self.file("alive", current)
        for name, mutation, uid in (
            ("owner", lambda: None, self.uid + 1),
            ("size", lambda: marker.write_bytes(b"x" * 129), self.uid),
            ("hardlink", lambda: os.link(marker, self.root / "linked"), self.uid),
            (
                "symlink",
                lambda: (marker.unlink(), marker.symlink_to(self.root / "target")),
                self.uid,
            ),
        ):
            with self.subTest(name=name):
                marker.unlink(missing_ok=True)
                (self.root / "linked").unlink(missing_ok=True)
                self.file("alive", current)
                mutation()
                self.refusal(
                    "runtime-boot-marker",
                    guards.boot_checkpoint,
                    marker,
                    current,
                    uid=uid,
                )

    def store_paths(self):
        return guards.StorePaths(
            self.root / "data/containers/storage",
            self.root / "podman-run",
            self.root / "data/containers/storage/volumes",
            self.root / "temporary",
            self.root / "config/containers/storage.conf",
        )

    def store_info(self):
        paths = self.store_paths()
        return {
            "host": {"security": {"rootless": True}, "serviceIsRemote": False},
            "store": {
                "graphDriverName": "overlay",
                **{
                    name: str(getattr(paths, name))
                    for name in paths.__dataclass_fields__
                },
            },
            "version": {"Version": "6.1.4"},
        }

    def test_store_info_shape_boolean_and_paths_matrix(self):
        info = self.store_info()
        paths = self.store_paths()
        guards.validate_store_info(info, paths)
        for value in (None, [], "private-value", True):
            self.refusal("store-info-shape", guards.validate_store_info, value, paths)
        for field in ("host", "store"):
            for value in (None, [], "private-value", True):
                with self.subTest(field=field, shape=type(value).__name__):
                    changed = copy.deepcopy(info)
                    changed[field] = value
                    self.refusal(
                        "store-info-shape", guards.validate_store_info, changed, paths
                    )
        for value in (None, [], "private-value", True):
            changed = copy.deepcopy(info)
            changed["host"]["security"] = value
            self.refusal("store-info-shape", guards.validate_store_info, changed, paths)
        for name, section, field, value, code in (
            ("rootful", "security", "rootless", False, "rootless"),
            ("numeric-rootless", "security", "rootless", 1, "rootless"),
            ("remote", "host", "serviceIsRemote", True, "remote-engine"),
            ("numeric-local", "host", "serviceIsRemote", 0, "remote-engine"),
            ("driver", "store", "graphDriverName", "vfs", "storage-driver"),
        ):
            with self.subTest(name=name):
                changed = copy.deepcopy(info)
                destination = (
                    changed["host"]["security"]
                    if section == "security"
                    else changed[section]
                )
                destination[field] = value
                self.refusal(code, guards.validate_store_info, changed, paths)
        for field in paths.__dataclass_fields__:
            code = (
                "image-temporary-path"
                if field == "imageCopyTmpDir"
                else ("store-info-shape" if field == "configFile" else "private-store")
            )
            for name, value in (
                ("missing", None),
                ("wrong-type", []),
                ("relative", "relative/path"),
                ("foreign", "/somewhere/else"),
                ("traversal", str(self.root / "a/../b")),
                ("control", str(getattr(paths, field)) + "\n"),
                ("trailing-slash", str(getattr(paths, field)) + "/"),
            ):
                with self.subTest(field=field, mutation=name):
                    changed = copy.deepcopy(info)
                    changed["store"][field] = value
                    self.refusal(code, guards.validate_store_info, changed, paths)

    def test_store_config_version_optin_matrix_and_no_probes(self):
        info = self.store_info()
        paths = self.store_paths()
        del info["store"]["configFile"]
        self.refusal("store-info-shape", guards.validate_store_info, info, paths)
        self.refusal(
            "store-info-shape",
            guards.validate_store_info,
            info,
            paths,
            allow_audited_614_omission=1,
        )
        self.refusal(
            "store-info-shape",
            guards.validate_store_info,
            info,
            replace(paths, configFile=Path("relative.conf")),
            allow_audited_614_omission=True,
        )
        for version in (
            None,
            [],
            {},
            {"version": "6.1.4"},
            {"Version": "6.1.4-dev"},
            {"Version": "6.1.5"},
            {"Version": 6.14},
            {"Version": "5.6.2"},
        ):
            with self.subTest(version=version):
                changed = copy.deepcopy(info)
                changed["version"] = version
                self.refusal(
                    "store-info-shape",
                    guards.validate_store_info,
                    changed,
                    paths,
                    allow_audited_614_omission=True,
                )
        with (
            patch.object(Path, "stat", side_effect=AssertionError("filesystem probe")),
            patch.object(Path, "lstat", side_effect=AssertionError("filesystem probe")),
            patch.object(os, "open", side_effect=AssertionError("file open")),
        ):
            guards.validate_store_info(info, paths, allow_audited_614_omission=True)
            info["store"]["configFile"] = "/foreign/storage.conf"
            self.refusal(
                "store-info-shape",
                guards.validate_store_info,
                info,
                paths,
                allow_audited_614_omission=True,
            )
            self.assertEqual(
                guards.private_selectors(self.root),
                {
                    "CONTAINERS_STORAGE_CONF": str(
                        self.root / "config/containers/storage.conf"
                    ),
                    "CONTAINERS_CONF": str(
                        self.root / "config/containers/containers.conf"
                    ),
                },
            )


if __name__ == "__main__":
    unittest.main()
