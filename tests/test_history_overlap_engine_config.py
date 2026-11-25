"""Synthetic filesystem evidence only; never invoke or configure Podman."""

from contextlib import ExitStack
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from oc_box import history_overlap_engine_config as config
from oc_box.history_overlap_order import OverlapRefusal


_ENGINE_MAIN = """\
# Active declarations of Fedora's modern containers.conf template.
[containers]
default_sysctls = ["net.ipv4.ping_group_range=0 0"]
[secrets]
[secrets.opts]
[network]
[engine]
[engine.runtimes]
[engine.runtimes_flags]
[engine.volume_plugins]
[machine]
[farms]
[podmansh]
"""
_STORAGE_MAIN = """\
[storage]
[storage.options]
[storage.options.pull_options]
[storage.options.overlay]
mountopt = "nodev"
[storage.options.vfs]
"""
_ENGINE_DROP = '[containers]\nlog_driver = "journald"\n[engine]\nruntime = "crun"\n'
_STORAGE_DROP = '[storage]\ndriver = "overlay"\n'
_ROOTLESS_DROP = '[storage.options.overlay]\nmountopt = "nodev"\n'
_MOUNTS = "/usr/share/rhel/secrets:/run/secrets\n"
_TOML = unittest.skipUnless(importlib.util.find_spec("tomllib"), "requires stdlib TOML")


class ConfigWitnessTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        private = stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(private)
        self.vendor = self.root / "usr/share/containers"
        self.site = self.root / "etc/containers"
        self.secrets = self.root / "usr/share/rhel/secrets"
        self.bin = self.root / "usr/bin"
        self.fips = self.root / "proc/sys/crypto/fips_enabled"
        self.home = self.root / "account"
        self.user = self.home / ".config/containers"
        self.bin.mkdir(parents=True)
        self.uid = os.geteuid() or 1000
        self.env = {
            "HOME": str(self.home),
            "PATH": str(self.bin),
            "XDG_RUNTIME_DIR": f"/run/user/{self.uid}",
        }
        # Use the actual filesystem and no-follow descriptor walks, but only
        # in a private synthetic tree owned by the test runner (root or not).
        stack.enter_context(
            patch.multiple(
                config,
                _SYSTEM=str(self.site.parent),
                _VENDOR=str(self.vendor),
                _SYSTEM_BIN=str(self.bin),
                _SECRETS=str(self.secrets),
                _FIPS=str(self.fips),
                _SYSTEM_UID=os.geteuid(),
            )
        )
        stack.enter_context(
            patch.object(
                config.pwd,
                "getpwuid",
                return_value=SimpleNamespace(
                    pw_dir=str(self.home), pw_name="synthetic"
                ),
            )
        )
        stack.enter_context(patch.dict(os.environ, {}, clear=True))

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def stock(self):
        self.write(self.vendor / "containers.conf", _ENGINE_MAIN)
        self.write(self.vendor / "storage.conf", _STORAGE_MAIN)
        self.write(self.vendor / "containers.conf.d/00-vendor.conf", _ENGINE_DROP)
        self.write(self.vendor / "storage.conf.d/00-vendor.conf", _STORAGE_DROP)
        self.write(
            self.vendor / "storage.rootless.conf.d/00-vendor-rootless.conf",
            _ROOTLESS_DROP,
        )
        # This distro file really does contain a non-rootless-compatible
        # additional store. It is not in the rootless loader's search path.
        self.write(
            self.vendor / "storage.rootful.conf.d/00-vendor-rootful.conf",
            '[storage.options]\nadditionalimagestores = ["/usr/lib/containers/storage"]\n'
            '[storage.options.overlay]\nmountopt = "nodev,metacopy=on"\n',
        )
        self.write(self.vendor / "mounts.conf", _MOUNTS)
        self.secrets.mkdir(parents=True)
        for name, path in (
            ("etc-pki-entitlement", "pki/entitlement"),
            ("rhsm", "rhsm"),
            ("redhat.repo", "yum.repos.d/redhat.repo"),
        ):
            (self.secrets / name).symlink_to("../../../../etc/" + path)
        (self.site / "oci/hooks.d").mkdir(parents=True)
        self.write(self.fips, "0\n")

    def report(self):
        graph = str(self.home / ".local/share/containers/storage")
        return {
            "host": {"security": {"rootless": True}, "serviceIsRemote": False},
            "plugins": {"volume": ["local"]},
            "store": {
                "graphRoot": graph,
                "runRoot": f"/run/user/{self.uid}/containers",
                "volumePath": graph + "/volumes",
                "graphDriverName": "overlay",
                "graphOptions": {"overlay.mountopt": "nodev"},
                "transientStore": False,
            },
        }

    def pin(self):
        return config.pin_config(self.env, self.uid)

    def assert_refused(self):
        with self.assertRaisesRegex(OverlapRefusal, r"\(engine-witness\)"):
            with self.pin():
                self.fail("unsupported witness was yielded")

    @_TOML
    def test_modern_vendor_files_and_stock_dangling_links_are_admitted(self):
        self.stock()
        # A known entitlement target may also be an empty directory.
        (self.site.parent / "pki/entitlement").mkdir(parents=True)
        report = self.report()
        with self.pin() as witness:
            self.assertEqual(dict(witness.environment), self.env)
            self.assertEqual(
                witness.require_local_store(report),
                tuple(
                    report["store"][key]
                    for key in ("graphRoot", "runRoot", "volumePath")
                ),
            )
            witness.recheck()
        with self.assertRaises(OverlapRefusal):
            witness.recheck()

    @_TOML
    def test_safe_site_main_files_and_both_configfile_report_shapes(self):
        self.stock()
        self.write(self.site / "containers.conf", "# retained no-op\n[engine]\n")
        site_storage = self.write(self.site / "storage.conf", _STORAGE_MAIN)
        report = self.report()
        report["store"]["graphOptions"] = {}
        with self.pin() as witness:
            witness.require_local_store(report)
            report["store"]["configFile"] = str(site_storage)
            witness.require_local_store(report)
            report["store"]["configFile"] = str(self.vendor / "storage.conf")
            with self.assertRaises(OverlapRefusal):
                witness.require_local_store(report)

    def test_absence_only_layout_and_environment_snapshot(self):
        with self.pin() as witness:
            self.env["PATH"] = "/new-untrusted-selection"
            self.assertEqual(witness.environment["PATH"], str(self.bin))
            witness.require_local_store(self.report())
            witness.recheck()
        self.assert_refused()

    def test_missing_stdlib_toml_refuses_only_layouts_requiring_it(self):
        # A Python 3.10-like import boundary: importing the module itself
        # needs no extra dependency, but config requiring TOML must refuse.
        with patch.dict(sys.modules, {"tomllib": None}):
            with self.pin() as witness:
                witness.require_local_store(self.report())
            self.stock()
            self.assert_refused()

    @_TOML
    def test_declared_routing_and_legacy_storage_are_refused_prequery(self):
        forbidden = (
            ("containers.conf", "[engine]\nremote = true\n"),
            ("containers.conf", '[engine]\nactive_service = "other"\n'),
            ("containers.conf", '[engine]\nnamespace = "other"\n'),
            ("containers.conf", '[engine]\nstatic_dir = "/other"\n'),
            ("containers.conf", '[engine]\nvolume_path = "/other"\n'),
            ("containers.conf", '[engine]\nhelper_binaries_dir = ["/other"]\n'),
            ("containers.conf", '[engine]\nhooks_dir = ["/other"]\n'),
            ("containers.conf", '[engine.runtimes]\ncrun = ["/other"]\n'),
            ("containers.conf", '[engine]\nenv = ["CONTAINERS_CONF=/other"]\n'),
            ("containers.conf", '[containers]\nlog_driver = "passthrough"\n'),
            ("containers.conf", '[engine]\nruntime = ["crun"]\n'),
            ("containers.conf", '[engine]\nruntime = "crun"\nruntime = "crun"\n'),
            ("containers.conf", "[unexpected]\n"),
            ("storage.conf", '[storage]\nrunroot = "/run/containers/storage"\n'),
            ("storage.conf", '[storage]\ngraphroot = "/var/lib/containers/storage"\n'),
            ("storage.conf", '[storage]\nrootless_storage_path = "/other"\n'),
            ("storage.conf", '[storage]\nimagestore = "/other"\n'),
            (
                "storage.conf",
                '[storage.options]\nadditionalimagestores = ["/usr/lib/containers/storage"]\n',
            ),
            ("storage.conf", '[storage.options]\nadditionallayerstores = ["/other"]\n'),
            ("storage.conf", '[storage.options.overlay]\nmount_program = "/other"\n'),
            (
                "storage.conf",
                '[storage.options.overlay]\nmountopt = "nodev,metacopy=on"\n',
            ),
            ("storage.conf", "[storage]\ntransient_store = true\n"),
        )
        for directory in (self.site, self.vendor):
            for name, contents in forbidden:
                with self.subTest(directory=directory.name, contents=contents):
                    path = self.write(directory / name, contents)
                    self.assert_refused()
                    path.unlink()

    def test_user_files_module_and_applicable_dropins_are_not_authority(self):
        uid = str(self.uid)
        paths = (
            self.user / "containers.conf",
            self.user / "podman-connections.json",
            self.user / "storage.conf",
            self.user / "storage.conf.d/00-vendor.conf",
            self.user / "containers.conf.modules/selected.conf",
            self.site / "containers.rootless.conf",
            self.site / "containers.conf.d/50-custom.conf",
            self.site / "storage.conf.d/00-vendor.conf",
            self.site / f"containers.rootless.conf.d/{uid}/00-vendor.conf",
            self.vendor / f"containers.rootless.conf.d/{uid}/00-vendor.conf",
            self.site / f"storage.rootless.conf.d/{uid}/00-vendor-rootless.conf",
            self.vendor / f"storage.rootless.conf.d/{uid}/00-vendor-rootless.conf",
            self.vendor / "containers.conf.d/01-extra.conf",
        )
        for path in paths:
            with self.subTest(path=path.relative_to(self.root)):
                self.write(path, "# no active keys; path itself is not admitted\n")
                self.assert_refused()
                path.unlink()
                # Home config directory presence itself is forbidden.
                for parent in path.parents:
                    if parent == self.user:
                        break
                    if self.user in parent.parents:
                        parent.rmdir()

    def test_no_inherited_or_passed_selector_can_be_silently_discarded(self):
        for name in (
            "CONTAINER_HOST",
            "CONTAINER_CONNECTION",
            "PODMAN_CONNECTIONS_CONF",
            "CONTAINERS_CONF",
            "CONTAINERS_CONF_OVERRIDE",
            "CONTAINERS_STORAGE_CONF_OVERRIDE",
            "STORAGE_DRIVER",
            "DOCKER_HOST",
            "XDG_CONFIG_DIRS",
        ):
            with self.subTest(name=name), patch.dict(os.environ, {name: ""}):
                self.assert_refused()
        for name, value in (
            ("HOME", "/different-account"),
            ("LOGNAME", "different-account"),
            ("PATH", str(self.bin) + ":/user/bin"),
            ("CONTAINERS_STORAGE_CONF", "/other"),
            ("XDG_RUNTIME_DIR", "/other"),
            ("XDG_CONFIG_HOME", "/other"),
            ("XDG_DATA_HOME", "/other"),
            ("DBUS_SESSION_BUS_ADDRESS", "tcp:host=other"),
            ("TMPDIR", "/other"),
        ):
            with self.subTest(name=name), patch.dict(self.env, {name: value}):
                self.assert_refused()

    def test_preexec_oci_and_implicit_mount_overrides_refuse(self):
        mutable_inputs = (
            self.site / "podman_preexec_hooks.txt",
            self.vendor / "oci/hooks.d/stock-looking.json",
            self.site / "oci/hooks.d/custom.json",
            self.user / "oci/hooks.d/custom.json",
            self.user / "mounts.conf",
            self.site / "mounts.conf",
            self.vendor / "mounts.conf",
        )
        for path in mutable_inputs:
            with self.subTest(path=path.relative_to(self.root)):
                self.write(path, "/synthetic/other:/run/secrets\n")
                self.assert_refused()
                path.unlink()
                if path.parent == self.user / "oci/hooks.d":
                    path.parent.rmdir()

    @_TOML
    def test_subscription_content_and_unverified_fips_refuse(self):
        self.stock()
        target = self.write(self.site.parent / "pki/entitlement/private", "synthetic")
        self.assert_refused()
        target.unlink()
        repo = self.write(self.site.parent / "yum.repos.d/redhat.repo", "# synthetic")
        self.assert_refused()
        repo.unlink()
        link = self.secrets / "rhsm"
        link.unlink()
        link.symlink_to("../../../../etc/other")
        self.assert_refused()
        link.unlink()
        self.write(self.fips, "1\n")
        self.assert_refused()

    def test_nofollow_special_hardlinked_and_writable_inputs_refuse(self):
        real = self.write(self.root / "benign", _ENGINE_MAIN)
        main = self.vendor / "containers.conf"
        main.parent.mkdir(parents=True)
        for replace in (
            lambda: main.symlink_to(real),
            lambda: main.symlink_to(self.root / "nonexistent"),
            lambda: main.hardlink_to(real),
            lambda: os.mkfifo(main),
        ):
            replace()
            self.assert_refused()
            main.unlink()
        self.write(main, _ENGINE_MAIN).chmod(0o666)
        self.assert_refused()
        main.chmod(0o644)
        self.vendor.chmod(0o1777)
        self.assert_refused()
        self.vendor.chmod(0o755)
        (self.site.parent).symlink_to(self.vendor, target_is_directory=True)
        self.assert_refused()

    def test_limits_apply_to_regular_files_and_candidate_directories(self):
        path = self.write(self.vendor / "containers.conf", "#" * (config._LIMIT + 1))
        self.assert_refused()
        path.unlink()
        hooks = self.site / "oci/hooks.d"
        for i in range(config._DIR_LIMIT + 1):
            self.write(hooks / f"{i}.ignored", "")
        self.assert_refused()

    @_TOML
    def test_changes_are_refused_and_witness_does_not_rearm(self):
        self.stock()
        for name, change, undo in (
            (
                "file rewrite",
                lambda: self.write(
                    self.vendor / "containers.conf", _ENGINE_MAIN + "# edit\n"
                ),
                lambda: self.write(self.vendor / "containers.conf", _ENGINE_MAIN),
            ),
            (
                "drop-in addition",
                lambda: self.write(
                    self.vendor / "containers.conf.d/01-custom.conf", ""
                ),
                lambda: (self.vendor / "containers.conf.d/01-custom.conf").unlink(),
            ),
            (
                "new negative path",
                lambda: self.write(self.user / "storage.conf", ""),
                lambda: (self.user / "storage.conf").unlink(),
            ),
            (
                "hook added",
                lambda: self.write(self.site / "oci/hooks.d/new.json", ""),
                lambda: (self.site / "oci/hooks.d/new.json").unlink(),
            ),
            (
                "subscription target appeared",
                lambda: self.write(self.site.parent / "yum.repos.d/redhat.repo", ""),
                lambda: (self.site.parent / "yum.repos.d/redhat.repo").unlink(),
            ),
        ):
            with self.subTest(name=name), self.pin() as witness:
                change()
                with self.assertRaises(OverlapRefusal):
                    witness.recheck()
                undo()
                with self.assertRaises(OverlapRefusal):
                    witness.recheck()

    @_TOML
    def test_matching_content_does_not_waive_pin_replacement(self):
        path = self.write(self.vendor / "containers.conf", _ENGINE_MAIN)
        with self.pin() as witness:
            replacement = self.write(path.with_suffix(".new"), _ENGINE_MAIN)
            replacement.replace(path)
            with self.assertRaises(OverlapRefusal):
                witness.recheck()

    def test_selector_appearance_refuses_an_existing_witness(self):
        with (
            self.pin() as witness,
            patch.dict(os.environ, {"PODMAN_PREEXEC_HOOKS_DIR": ""}),
        ):
            with self.assertRaises(OverlapRefusal):
                witness.recheck()

    def test_change_during_directory_scan_refuses_before_yield(self):
        directory = self.site / "oci/hooks.d"
        directory.mkdir(parents=True)
        original_names = config._names

        def mutate_after_scan(pin):
            names = original_names(pin)
            if pin.path == str(directory):
                self.write(directory / "appeared.json", "{}")
            return names

        with patch.object(config, "_names", side_effect=mutate_after_scan):
            self.assert_refused()

    def test_effective_info_does_not_waive_unknown_routes_or_options(self):
        for section, key, value in (
            ("host", "serviceIsRemote", True),
            ("host", "security", {"rootless": 1}),
            ("plugins", "volume", ["local", "external"]),
            ("store", "graphDriverName", "vfs"),
            ("store", "transientStore", True),
            ("store", "graphRoot", "/elsewhere"),
            ("store", "runRoot", "/elsewhere"),
            ("store", "volumePath", "/elsewhere/volumes"),
            ("store", "graphOptions", None),
            (
                "store",
                "graphOptions",
                {"overlay.mount_program": {"Executable": "/other"}},
            ),
            ("store", "graphOptions", {"overlay.additionalImageStores": ["/other"]}),
            ("store", "configFile", str(self.user / "storage.conf")),
        ):
            with (
                self.subTest(section=section, key=key, value=value),
                self.pin() as witness,
            ):
                report = self.report()
                report[section][key] = value
                with self.assertRaises(OverlapRefusal):
                    witness.require_local_store(report)
                with self.assertRaises(OverlapRefusal):
                    witness.require_local_store(self.report())


if __name__ == "__main__":
    unittest.main()
