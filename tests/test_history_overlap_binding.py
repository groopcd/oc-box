"""Verify exact volume observation and namespace acquisition ordering offline."""

import contextlib
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import (
    history_overlap_binding as binding,
    history_overlap_domains as domains,
)


class QuietVolumeBindingTests(unittest.TestCase):
    def setUp(self):
        self.data = {
            "id": "a" * 32,
            "container": "b" * 64,
            "opencode_version": "2.4.1",
            "project": "/synthetic/project",
            "home": "/synthetic/home",
            "home_volume": "synthetic_home",
        }
        backing = "/synthetic/engine/home"
        mounts = (
            self.data["project"],
            self.data["project"],
            backing,
            self.data["home"],
        )
        self.binding = SimpleNamespace(
            container=self.data["container"], mounts=mounts, start_verified=True
        )
        self.box = SimpleNamespace(
            LABEL="synthetic.workspace",
            container_project=lambda data: data["project"],
            _quiet_sync_mounts=lambda *_: mounts,
        )
        self.info = {
            "Id": self.data["container"],
            "Config": {"Labels": {self.box.LABEL: self.data["id"]}},
            "State": {
                "Running": True,
                "Status": "running",
                "Pid": 42,
                "StartedAt": "2026-01-01T00:00:00Z",
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Destination": self.data["project"],
                    "Source": self.data["project"],
                },
                {
                    "Type": "volume",
                    "Destination": self.data["home"],
                    "Source": backing,
                    "Name": self.data["home_volume"],
                },
            ],
        }
        self.volume = {
            "Name": self.data["home_volume"],
            "Driver": "local",
            "Scope": "local",
            "Mountpoint": backing,
            "Options": {},
            "Labels": {self.box.LABEL: self.data["id"]},
        }

    def acquire(self, *, inspect=None, volume=None, check=None):
        return binding.pin_quiet_volume(
            self.box,
            self.data,
            self.binding,
            inspect=inspect or (lambda: self.info),
            inspect_volume=volume or (lambda: self.volume),
            check=check or (lambda: None),
        )

    def test_exact_backing_is_checked_before_producing_a_scope(self):
        bound = Mock()
        check = Mock()
        with (
            patch.object(domains, "observe_process_start", return_value=19) as observe,
            patch.object(
                domains,
                "pin_current_namespace",
                return_value=contextlib.nullcontext("host"),
            ),
            patch.object(
                domains,
                "pin_process_namespace",
                return_value=contextlib.nullcontext("init"),
            ) as pin,
            patch.object(
                domains, "pin_mount_backing", return_value=contextlib.nullcontext(bound)
            ) as backing,
            self.acquire(check=check) as selected,
        ):
            self.assertIs(selected, bound)
        observe.assert_called_once_with(42)
        pin.assert_called_once_with(42, start_ticks=19)
        backing.assert_called_once_with(
            "init", self.data["home"], "host", self.volume["Mountpoint"]
        )
        bound.recheck.assert_called_once()
        self.assertEqual(check.call_count, 2)

    def test_unowned_or_optioned_volume_never_reaches_namespace_acquisition(self):
        for field, unverified in (
            ("Options", {"device": "/synthetic/root"}),
            ("Scope", "remote"),
            ("Mountpoint", "/synthetic/other"),
            ("Labels", {}),
        ):
            volume = {**self.volume, field: unverified}
            with (
                self.subTest(field=field),
                patch.object(domains, "observe_process_start") as observe,
                self.assertRaises(domains.DomainRefusal),
            ):
                with self.acquire(volume=lambda: volume):
                    self.fail("A foreign volume supplied scratch authority")
            observe.assert_not_called()

    def test_generation_change_after_pin_refuses_early_evidence(self):
        changed = copy.deepcopy(self.info)
        changed["State"]["StartedAt"] = "2026-01-02T00:00:00Z"
        with (
            patch.object(domains, "observe_process_start", return_value=19),
            patch.object(
                domains,
                "pin_current_namespace",
                return_value=contextlib.nullcontext("host"),
            ),
            patch.object(
                domains,
                "pin_process_namespace",
                return_value=contextlib.nullcontext("init"),
            ),
            patch.object(
                domains,
                "pin_mount_backing",
                return_value=contextlib.nullcontext(Mock()),
            ),
            self.assertRaises(domains.DomainRefusal),
        ):
            with self.acquire(inspect=Mock(side_effect=[self.info, changed])):
                self.fail("Same ID with another start generation was accepted")


class ArchiveLayoutTests(unittest.TestCase):
    def env(self):
        return {
            "HOME": "/synthetic/scratch/home",
            "OPENCODE_TEST_HOME": "/synthetic/scratch/home",
            "XDG_DATA_HOME": "/synthetic/data",
            "XDG_CONFIG_HOME": "/synthetic/scratch/config",
            "XDG_STATE_HOME": "/synthetic/scratch/state",
            "XDG_CACHE_HOME": "/synthetic/scratch/cache",
            "XDG_RUNTIME_DIR": "/synthetic/scratch/runtime",
            "TMPDIR": "/synthetic/scratch/temp",
        }

    def test_exact_ordinary_host_and_quiet_source_keep_distinct_native_selectors(self):
        host = binding.writable_layout(
            "2.4.0", self.env(), "/synthetic/controller", role="host"
        )
        source = binding.writable_layout(
            "2.4.1",
            {**self.env(), "OPENCODE_DB": "oc-box-v2.db"},
            "/synthetic/scratch/work",
            role="source",
        )
        self.assertEqual(host.database, "/synthetic/data/opencode/opencode.db")
        self.assertEqual(source.database, "/synthetic/data/opencode/oc-box-v2.db")
        self.assertEqual(set(dict(host.roots)), domains._SCOPES)
        self.assertEqual(dict(source.roots)["cwd"], "/synthetic/scratch/work")
        self.assertEqual(
            dict(host.roots)["home-fallback"], "/synthetic/scratch/home/.local"
        )

    def test_alternate_db_and_broad_scratch_root_never_qualify(self):
        for override in (
            {"OPENCODE_DB": "/synthetic/data/other.db"},
            {"TMPDIR": "/synthetic/scratch"},
            {"OPENCODE_TEST_HOME": "/other/home"},
            {"XDG_RUNTIME_DIR": ""},
        ):
            with (
                self.subTest(override=override),
                self.assertRaises(domains.DomainRefusal),
            ):
                binding.writable_layout(
                    "2.4.1",
                    {**self.env(), **override},
                    "/synthetic/controller",
                    role="host",
                )

    def test_source_ready_record_cannot_select_another_scratch_or_database(self):
        data = {"home": "/synthetic/home", "opencode_version": "2.4.1"}
        cwd = data["home"] + "/.oc-box-quiet-history-" + "a" * 32 + "/work"
        selected = binding.source_layout(data, {"pid": 42, "cwd": cwd})
        self.assertEqual(
            selected.database, data["home"] + "/.local/share/opencode/oc-box-v2.db"
        )
        self.assertEqual(dict(selected.roots)["cwd"], cwd)
        for update in ({"cwd": "/tmp/work"}, {"pid": 1}, {"database": "/elsewhere.db"}):
            with self.subTest(update=update), self.assertRaises(domains.DomainRefusal):
                binding.source_layout(data, {**{"pid": 42, "cwd": cwd}, **update})
