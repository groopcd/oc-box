import contextlib
from importlib import metadata
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from oc_box import package_updates as updates
from oc_box import update_channels as channels


def public_file(version, **changes):
    value = {
        "filename": f"oc_box-{version}-py3-none-any.whl",
        "url": f"https://files.pythonhosted.org/packages/fixture/oc_box-{version}-py3-none-any.whl",
        "requires-python": ">=3.10",
        "yanked": False,
    }
    value.update(changes)
    return value


class PackageUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-packages-test-")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.prefix = self.work / "tools/oc-box"
        self.root = self.prefix / "lib/python3/site-packages/oc_box"
        self.root.mkdir(parents=True)
        (self.prefix / "bin").mkdir()
        (self.prefix / "bin/python").touch()
        (self.prefix / "pyvenv.cfg").write_text(
            "include-system-site-packages = false\n"
        )
        (self.root / "distribution.json").write_text(
            '{"installer":"python-package","version":"1.6.1"}'
        )
        self.before = {
            "prefix": str(self.prefix),
            "base": "/base",
            "version": "1.6.1",
            "library": str(self.root.parent),
            "package": str(self.root),
            "direct": False,
            "requested": True,
            "extras": [],
        }
        self.extras = ["vault", "tui"]
        self.calls = []
        self.files = [
            public_file("1.6.1"),
            public_file("1.6.2"),
            public_file("1.6.3.dev0"),
        ]
        self.dist = self.distribution("1.6.1")
        self.patches = [
            patch.object(updates.os, "geteuid", return_value=1000),
            patch.object(updates.sys, "prefix", str(self.prefix)),
            patch.object(updates.sys, "base_prefix", "/base"),
            patch.object(updates, "_private", return_value=True),
            patch.object(updates.metadata, "distribution", return_value=self.dist),
            patch.object(
                updates,
                "_operation_lock",
                side_effect=lambda *a: contextlib.nullcontext(),
            ),
            patch.object(updates, "_probe", side_effect=self.probe),
            patch.object(updates, "_manager", side_effect=self.manager),
            patch.object(updates, "_run", side_effect=self.run_manager),
            patch.object(channels, "fetch_json", side_effect=self.fetch),
            patch.dict(
                os.environ,
                {"OC_BOX_CONTAINER": "", "XDG_CONFIG_HOME": str(self.work / "config")},
            ),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def distribution(self, version):
        class Distribution:
            def read_text(_, name):
                return None

            def locate_file(_, name):
                return self.root if name == "oc_box" else self.root.parent

        value = Distribution()
        value.version = version
        return value

    def probe(self, *args):
        return dict(self.before)

    def manager(self, *args):
        return "uv", Path("/verified/uv"), self.extras

    def run_manager(self, argv, work, env, **kwargs):
        self.calls.append((argv, dict(env), kwargs))
        pin = next(x for x in map(str, argv) if x.startswith("oc-box") and "==" in x)
        self.before["version"] = pin.split("==")[1]
        return ""

    def fetch(self, url):
        if url == updates.SIMPLE:
            return {"files": self.files}
        version = url.split("/")[-2]
        return {
            "info": {
                "name": "oc-box",
                "version": version,
                "requires_python": ">=3.10",
                "provides_extra": ["vault", "tui"]
                if version.endswith(".dev0")
                else ["vault"],
            },
            "urls": [public_file(version)],
        }

    def upgrade(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertIsNone(updates.upgrade(self.root, **kwargs))
        return output.getvalue()

    def settings(self):
        return channels.load_settings()

    def test_first_selection_is_stable_exact_and_saves_only_package_preference(self):
        output = self.upgrade()
        self.assertIn("PyPI stable: 1.6.2", output)
        self.assertIn("Newer development version available: 1.6.3.dev0", output)
        command, env, options = self.calls[0]
        self.assertIn("oc-box[vault]==1.6.2", command)
        self.assertIn("--force", command)
        self.assertIn("--no-build", command)
        self.assertNotIn("--pre", command)
        self.assertEqual(options["timeout"], 240)
        self.assertEqual(
            list(self.settings()["package_update_channels"].values()), ["stable"]
        )
        self.assertEqual(
            list(self.settings()["package_update_extras"].values()), [["tui", "vault"]]
        )
        self.assertNotIn("update_channel", self.settings())
        self.assertTrue(env["PIP_CONSTRAINT"].endswith("target.txt"))

    def test_explicit_devel_check_is_preview_and_no_preferences_or_lock_written(self):
        output = self.upgrade(check=True, channel="devel")
        self.assertIn("PyPI devel: 1.6.3.dev0 (check only)", output)
        self.assertFalse(self.calls)
        self.assertEqual(self.settings(), {})
        self.assertFalse((self.prefix / ".oc-box-upgrade.lock").exists())

    def test_current_stable_still_warns_and_explicit_flag_saves_verified_preference(
        self,
    ):
        self.files = [public_file("1.6.1"), public_file("1.6.2.dev0")]
        output = self.upgrade(channel="stable")
        self.assertIn("up to date", output)
        self.assertIn("Newer development", output)
        self.assertFalse(self.calls)
        self.assertEqual(
            list(self.settings()["package_update_channels"].values()), ["stable"]
        )

    def test_ahead_guard_and_explicit_stable_downgrade(self):
        self.files = [public_file("1.6.0"), public_file("1.6.0.dev0")]
        output = self.upgrade()
        self.assertIn("ahead", output)
        self.assertFalse(self.calls)
        self.assertEqual(self.settings(), {})
        self.upgrade(channel="stable")
        self.assertIn("oc-box[vault]==1.6.0", self.calls[0][0])

    def test_explicit_switch_to_older_devel_allowed_but_repeated_devel_guarded(self):
        self.files = [public_file("1.6.0.dev0")]
        self.upgrade(channel="devel")
        self.assertIn("oc-box[tui,vault]==1.6.0.dev0", self.calls[0][0])
        self.calls.clear()
        self.before["version"] = "1.6.1"
        self.upgrade(channel="devel")
        self.assertFalse(self.calls)

    def test_failed_or_false_success_never_saves_preferences(self):
        for failure in ("child", "unchanged", "wrong-prefix", "source"):
            with self.subTest(failure=failure):
                self.before["version"] = "1.6.1"

                def operation(argv, work, env, **kwargs):
                    if failure == "child":
                        raise channels.ChannelError("fixed failure")
                    if failure == "wrong-prefix":
                        self.before["prefix"] = "/foreign"
                    if failure == "source":
                        self.before["direct"] = True
                    return ""

                with patch.object(updates, "_run", side_effect=operation):
                    with self.assertRaises(channels.ChannelError):
                        self.upgrade(channel="devel")
                self.assertEqual(self.settings(), {})
                self.before["prefix"] = str(self.prefix)
                self.before["direct"] = False

    def test_warning_failure_is_nonblocking_and_older_devel_is_not_newer(self):
        original = self.fetch

        def discovery(url):
            if ".dev0/" in url:
                raise channels.ChannelError("private synthetic error")
            return original(url)

        with (
            patch.object(channels, "fetch_json", side_effect=discovery),
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            self.upgrade()
        self.assertIn("warning unavailable", errors.getvalue())
        self.assertNotIn("private synthetic", errors.getvalue())
        self.before["version"] = "1.6.1"
        self.files = [public_file("1.6.1"), public_file("1.6.1.dev0")]
        self.assertNotIn("Newer development", self.upgrade())

    def test_unknown_system_source_and_external_owners_refuse_before_network(self):
        cases = [
            (patch.object(updates.sys, "base_prefix", str(self.prefix)), "System"),
            (patch.object(updates.os, "geteuid", return_value=0), "without sudo"),
            (patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}), "host"),
            (
                patch.object(
                    self.dist,
                    "read_text",
                    return_value='{"dir_info":{"editable":true}}',
                ),
                "Editable",
            ),
        ]
        for condition, message in cases:
            with (
                self.subTest(message=message),
                condition,
                patch.object(channels, "fetch_json") as network,
            ):
                with self.assertRaisesRegex(channels.ChannelError, message):
                    updates.upgrade(self.root)
                network.assert_not_called()
        (self.prefix / "EXTERNALLY-MANAGED").touch()
        with patch.object(channels, "fetch_json") as network:
            with self.assertRaisesRegex(channels.ChannelError, "externally"):
                updates.upgrade(self.root)
            network.assert_not_called()

    def test_extra_preference_survives_stable_without_tui_then_returns_on_devel(self):
        self.upgrade(channel="stable")
        (self.root / "distribution.json").write_text(
            '{"installer":"python-package","version":"1.6.2"}'
        )
        self.dist.version = "1.6.2"
        self.extras = ["vault"]
        self.upgrade(channel="devel")
        self.assertIn("oc-box[tui,vault]==1.6.3.dev0", self.calls[-1][0])

    def test_missing_non_tui_extra_is_not_silently_removed(self):
        self.extras.append("other")
        with self.assertRaisesRegex(
            channels.ChannelError, "all originally requested extras"
        ):
            self.upgrade()
        self.assertFalse(self.calls)
        self.assertEqual(self.settings(), {})

    def test_channel_and_extras_save_together_under_operation_lock(self):
        channels.save_settings(
            {
                "pass_env": ["PUBLIC_SETTING"],
                "auto_update": True,
                "update_channel": "devel",
            }
        )
        active = []

        @contextlib.contextmanager
        def lock(prefix, check):
            active.append(True)
            try:
                yield
            finally:
                active.pop()

        mutate = channels.mutate_settings

        def remember(change):
            self.assertTrue(active)
            return mutate(change)

        with (
            patch.object(updates, "_operation_lock", side_effect=lock),
            patch.object(channels, "mutate_settings", side_effect=remember) as write,
        ):
            self.upgrade(channel="stable")
        self.assertEqual(write.call_count, 1)
        saved = self.settings()
        self.assertEqual(saved["pass_env"], ["PUBLIC_SETTING"])
        self.assertIs(saved["auto_update"], True)
        self.assertEqual(saved["update_channel"], "devel")
        self.assertEqual(list(saved["package_update_channels"].values()), ["stable"])
        self.assertEqual(
            list(saved["package_update_extras"].values()), [["tui", "vault"]]
        )

    def test_receipt_not_stale_preference_controls_non_tui_extras(self):
        self.upgrade(channel="stable")
        (self.root / "distribution.json").write_text(
            '{"installer":"python-package","version":"1.6.2"}'
        )
        self.dist.version = "1.6.2"
        self.extras = []
        self.upgrade(channel="devel")
        self.assertIn("oc-box[tui]==1.6.3.dev0", self.calls[-1][0])

    def test_discovery_ignores_yanked_unsupported_python_sources_and_other_prereleases(
        self,
    ):
        self.files.extend(
            [
                public_file("9.0.0", yanked="bad"),
                public_file("8.0.0", **{"requires-python": ">=99"}),
                public_file("7.0.0", url="https://private.invalid/a.whl"),
                public_file(
                    "6.0.0", url="https://user:secret@files.pythonhosted.org/a.whl"
                ),
                public_file("5.0.0", filename="oc_box-5.0.0rc1-py3-none-any.whl"),
                public_file("4.0.0", filename="oc_box-4.0.0.dev1-py3-none-any.whl"),
                public_file("3.0.0", filename="oc_box-3.0.0.tar.gz"),
            ]
        )
        self.assertIn("PyPI stable: 1.6.2", self.upgrade(check=True))

    def test_metadata_python_yanked_and_extras_checks_and_bounded_inventory(self):
        original = self.fetch

        def discovery(url):
            result = original(url)
            if "/1.6.2/json" in url:
                result["urls"][0]["yanked"] = True
            return result

        with patch.object(channels, "fetch_json", side_effect=discovery):
            self.assertIn("PyPI stable: 1.6.1", self.upgrade(check=True))
        with patch.object(
            channels,
            "fetch_json",
            return_value={"files": [{}] * (updates.MAX_FILES + 1)},
        ):
            with self.assertRaisesRegex(channels.ChannelError, "oversized"):
                self.upgrade(check=True)


class ManagerSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-manager-test-")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.prefix = self.work / "venvs/oc-box"
        (self.prefix / "bin").mkdir(parents=True)
        (self.prefix / "bin/oc-box").touch()
        (self.prefix / "bin/python").touch()
        self.bin = self.work / "bin"
        self.bin.mkdir()
        (self.bin / "oc-box").symlink_to(self.prefix / "bin/oc-box")
        self.before = {"version": "1.6.1", "extras": [], "requested": True}
        self.env = updates._environment(self.work)

    def receipt(self):
        return {
            "pipx_metadata_version": "0.5",
            "venv_args": [],
            "injected_packages": {},
            "source_interpreter": {
                "__Path__": str(self.prefix / "bin/python"),
                "__type__": "Path",
            },
            "main_package": {
                "package": "oc-box",
                "package_version": "1.6.1",
                "package_or_url": "oc-box[tui,vault]==1.6.1",
                "include_dependencies": False,
                "include_apps": True,
                "pip_args": [],
                "apps": ["oc-box"],
                "suffix": "",
                "pinned": False,
            },
        }

    def pipx(self, receipt=None, *, inventory=None, actual=None):
        receipt = receipt or self.receipt()
        (self.prefix / "pipx_metadata.json").write_text(json.dumps(receipt))

        def run(argv, *args, **kwargs):
            if "list" in argv:
                return json.dumps(
                    inventory
                    if inventory is not None
                    else {"venvs": {"oc-box": {"metadata": receipt}}}
                )
            return str(actual or self.prefix.parent)

        with (
            patch.dict(os.environ, {"PIPX_BIN_DIR": str(self.bin)}),
            patch.object(updates, "_executable", return_value=Path("/verified/pipx")),
            patch.object(updates, "_run", side_effect=run),
        ):
            return updates._manager(self.prefix, self.before, self.work, self.env)

    def test_pipx_verifies_inventory_prefix_launcher_extras_and_options(self):
        kind, _, extras = self.pipx()
        self.assertEqual((kind, extras), ("pipx", ["tui", "vault"]))
        self.assertEqual(self.env["PIPX_HOME"], str(self.prefix.parent.parent))
        for changes in (
            {"pip_args": ["--extra-index-url", "https://private.invalid"]},
            {"pinned": True},
            {"suffix": "-other"},
            {"package_version": "0.0.0"},
            {"package_or_url": "git+https://example.invalid/repo"},
        ):
            receipt = self.receipt()
            receipt["main_package"].update(changes)
            with (
                self.subTest(changes=changes),
                self.assertRaises(channels.ChannelError),
            ):
                self.pipx(receipt)
        for kwargs in ({"inventory": {"venvs": {}}}, {"actual": self.work / "other"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(channels.ChannelError):
                self.pipx(**kwargs)

    def test_pipx_custom_environment_injections_and_marker_only_are_refused(self):
        for key, value in (
            ("venv_args", ["--system-site-packages"]),
            ("injected_packages", {"foreign": {}}),
        ):
            receipt = self.receipt()
            receipt[key] = value
            with self.subTest(key=key), self.assertRaises(channels.ChannelError):
                self.pipx(receipt)
        (self.prefix / "pipx_metadata.json").write_text("{}")
        with self.assertRaises(channels.ChannelError):
            updates._manager(self.prefix, self.before, self.work, self.env)

    def test_plain_pip_refuses_unrecorded_extras_and_indirect_installs(self):
        for change in ({"extras": ["vault"]}, {"requested": False}):
            before = dict(self.before, **change)
            with self.subTest(change=change), patch.object(updates, "_run") as run:
                with self.assertRaises(channels.ChannelError):
                    updates._manager(self.prefix, before, self.work, self.env)
                run.assert_not_called()
        with patch.object(updates, "_run", return_value="pip 25") as run:
            self.assertEqual(
                updates._manager(self.prefix, self.before, self.work, self.env)[0],
                "pip",
            )
        self.assertIn("-I", run.call_args.args[0])

    @unittest.skipIf(
        sys.version_info < (3, 11), "uv receipt parsing needs stdlib tomllib"
    )
    def test_uv_receipt_options_and_inventory_are_verified_not_just_markers(self):
        basic = (
            '[tool]\nrequirements = [{name="oc-box", extras=["vault"], specifier=">=1.6.1"}]\n'
            'entrypoints = [{name="oc-box", install-path='
            + json.dumps(str(self.bin / "oc-box"))
            + ', from="oc-box"}]\n'
        )
        receipt = self.prefix / "uv-receipt.toml"
        receipt.write_text(basic)

        def run(argv, *args, **kwargs):
            return (
                str(self.prefix.parent)
                if "dir" in argv
                else f"oc-box v1.6.1 ({self.prefix})"
            )

        with (
            patch.object(updates, "_executable", return_value=Path("/verified/uv")),
            patch.object(updates, "_run", side_effect=run),
        ):
            self.assertEqual(
                updates._manager(self.prefix, self.before, self.work, self.env)[2],
                ["vault"],
            )
            # uv records the exact interpreter selector written by our own
            # delegation. It must remain reusable, not admit other interpreters.
            receipt.write_text(
                basic + "python = " + json.dumps(str(self.prefix / "bin/python")) + "\n"
            )
            self.assertEqual(
                updates._manager(self.prefix, self.before, self.work, self.env)[2],
                ["vault"],
            )
            for value in (
                basic + 'python = "/foreign/bin/python"\n',
                basic + 'python = "3.13"\n',
                basic + 'python = ["unverified"]\n',
                basic + '[tool.options]\nindex-url = "https://private.invalid"\n',
                basic + '[tool.options]\nresolution = "lowest"\n',
                basic.replace('name="oc-box", extras=', 'name="other", extras='),
                basic.replace('specifier=">=1.6.1"', 'url="file:///private"'),
                basic.replace(
                    '[{name="oc-box", extras=["vault"], specifier=">=1.6.1"}]',
                    '[{name="oc-box"}, {name="foreign"}]',
                ),
            ):
                receipt.write_text(value)
                with (
                    self.subTest(value=value),
                    self.assertRaises(channels.ChannelError),
                ):
                    updates._manager(self.prefix, self.before, self.work, self.env)
            receipt.write_text(basic)
            with patch.object(updates, "_run", return_value="/other"):
                with self.assertRaisesRegex(channels.ChannelError, "different tools"):
                    updates._manager(self.prefix, self.before, self.work, self.env)
        with patch.dict(sys.modules, {"tomllib": None}):
            with self.assertRaisesRegex(channels.ChannelError, "3.11"):
                updates._manager(self.prefix, self.before, self.work, self.env)

    def test_own_uv_options_are_reusable_but_custom_options_are_not(self):
        self.assertTrue(updates._uv_options({}))
        self.assertTrue(
            updates._uv_options({"index-url": updates.INDEX, "no-build": True})
        )
        self.assertFalse(updates._uv_options({"index-url": "https://private.invalid"}))
        self.assertFalse(updates._uv_options({"no-build": False}))
        self.assertFalse(updates._uv_options({"prerelease": "allow"}))

    def test_child_environment_stdin_timeout_and_private_errors(self):
        hostile = {
            "PERSONAL_GITHUB_TOKEN": "private",
            "GH_TOKEN": "private",
            "GITHUB_TOKEN": "private",
            "OPENAI_API_KEY": "private",
            "HTTP_PROXY": "http://secret.invalid",
            "HTTPS_PROXY": "http://secret.invalid",
            "PIP_INDEX_URL": "https://secret.invalid",
            "UV_INDEX": "https://secret.invalid",
            "LD_PRELOAD": "/secret",
            "PYTHONPATH": "/secret",
            "CUSTOM_SECRET": "private",
            "UV_TOOL_DIR": "/foreign",
            "PIPX_HOME": "/foreign",
        }
        with patch.dict(os.environ, hostile):
            env = updates._environment(self.work)
        self.assertFalse(set(hostile) & set(env))
        with patch.object(
            updates.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "ok", ""),
        ) as run:
            self.assertEqual(updates._run(["/verified/manager"], self.work, env), "ok")
        self.assertEqual(run.call_args.kwargs["input"], "")
        self.assertEqual(run.call_args.kwargs["timeout"], 20)
        for failure in (
            subprocess.CompletedProcess([], 1, "private-output", "private-error"),
            subprocess.TimeoutExpired(["private-argv"], 1),
        ):
            kwargs = (
                {"side_effect": failure}
                if isinstance(failure, Exception)
                else {"return_value": failure}
            )
            with patch.object(updates.subprocess, "run", **kwargs):
                with self.assertRaises(channels.ChannelError) as error:
                    updates._run(["/verified/manager"], self.work, env)
            self.assertNotIn("private-", str(error.exception))

    def test_lock_serializes_whole_operation_and_check_has_no_filesystem_effect(self):
        with updates._operation_lock(self.prefix, True):
            pass
        self.assertFalse(list(self.prefix.glob(".oc-box*")))
        with updates._operation_lock(self.prefix, False):
            with self.assertRaisesRegex(channels.ChannelError, "in progress"):
                with updates._operation_lock(self.prefix, False):
                    self.fail("concurrent lock acquired")
        with updates._operation_lock(self.prefix, False):
            pass
        lock = next(self.prefix.parent.glob(".oc-box-upgrade-*.lock"))
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    def test_requires_python_conservative_matrix(self):
        current = ".".join(map(str, sys.version_info[:3]))
        for specifier, expected in (
            (">=3.10", True),
            ("<3", False),
            ("~=3.10", True),
            ("==3.*", True),
            ("!=3.*", False),
            ("==" + current, True),
            (">=3.10, <4", True),
            ("==private", False),
            ("<99", True),
            ("~=3", False),
            (">=3.*", False),
        ):
            with self.subTest(specifier=specifier):
                self.assertEqual(updates._python_compatible(specifier), expected)

    def test_receipt_symlinks_and_manager_executable_mismatches_refuse(self):
        real = self.work / "receipt"
        real.write_text("{}")
        (self.prefix / "pipx_metadata.json").symlink_to(real)
        with self.assertRaises(channels.ChannelError):
            updates._manager(self.prefix, self.before, self.work, self.env)
        executable = self.work / "uv"
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o755)
        with (
            patch.object(updates.shutil, "which", return_value=str(executable)),
            patch.object(updates, "_run", return_value="not uv"),
        ):
            with self.assertRaisesRegex(channels.ChannelError, "version"):
                updates._executable("uv", self.work, self.env)


class IsolatedManagerTests(unittest.TestCase):
    """Actual wheel replacement in disposable venvs; only transport is offline."""

    def setUp(self):
        if not shutil.which("uv"):
            self.skipTest("An existing uv is needed for isolated wheel fixture setup.")
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-real-package-test-")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.wheels = self.work / "wheels"
        self.wheels.mkdir()
        self.uv = Path(shutil.which("uv")).resolve()
        self.env = updates._environment(self.work)
        self.env["UV_TOOL_DIR"] = str(self.work / "tools")
        self.env["UV_TOOL_BIN_DIR"] = str(self.work / "bin")
        for version in ("1.6.1", "1.6.2", "1.6.3.dev0"):
            self.wheel(version)

    def wheel(self, version):
        dist = f"oc_box-{version}.dist-info"
        files = {
            "oc_box/__init__.py": "",
            "oc_box/fixture.py": "def main(): pass\n",
            "oc_box/distribution.json": json.dumps(
                {"installer": "python-package", "version": version}
            ),
            f"{dist}/METADATA": f"Metadata-Version: 2.3\nName: oc-box\nVersion: {version}\nRequires-Python: >=3.10\n",
            f"{dist}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            f"{dist}/entry_points.txt": "[console_scripts]\noc-box = oc_box.fixture:main\n",
            f"{dist}/RECORD": "",
        }
        files[f"{dist}/RECORD"] = "".join(name + ",,\n" for name in files)
        with zipfile.ZipFile(
            self.wheels / f"oc_box-{version}-py3-none-any.whl", "w"
        ) as archive:
            for name, value in files.items():
                archive.writestr(name, value)

    def command(self, argv, env=None):
        result = subprocess.run(
            list(map(str, argv)),
            cwd=self.work,
            env=env or self.env,
            input="",
            text=True,
            capture_output=True,
            timeout=40,
        )
        self.assertEqual(result.returncode, 0, "Isolated fixture command failed.")
        return result.stdout.strip()

    def discovery(self, url):
        if url == updates.SIMPLE:
            return {
                "files": [
                    public_file("1.6.1"),
                    public_file("1.6.2"),
                    public_file("1.6.3.dev0"),
                ]
            }
        version = url.split("/")[-2]
        return {
            "info": {"name": "oc-box", "version": version, "provides_extra": []},
            "urls": [public_file(version)],
        }

    def normalize_receipt_transport(self, prefix, kind):
        """Undo only the fixture's offline index, never manager owner/options."""
        if kind == "pipx":
            receipt = prefix / "pipx_metadata.json"
            before = json.loads(receipt.read_text())
            flags = before["main_package"]["pip_args"]
            position = flags.index("--no-index")
            self.assertEqual(flags.count("--no-index"), 1)
            self.assertEqual(
                flags[position : position + 3],
                ["--no-index", "--find-links", str(self.wheels)],
            )
            after = json.loads(receipt.read_text())
            after["main_package"]["pip_args"][position : position + 3] = [
                "--index-url",
                updates.INDEX,
            ]
            receipt.write_text(json.dumps(after))
            normalized = json.loads(receipt.read_text())
            self.assertEqual(
                normalized["main_package"]["pip_args"],
                flags[:position]
                + ["--index-url", updates.INDEX]
                + flags[position + 3 :],
            )
            normalized["main_package"]["pip_args"] = flags
            self.assertEqual(normalized, before)
        elif kind == "uv":
            import tomllib

            receipt = prefix / "uv-receipt.toml"
            text = receipt.read_text()
            before = tomllib.loads(text)
            options = before["tool"]["options"]
            self.assertIs(options["no-index"], True)
            self.assertEqual(options["find-links"], [self.wheels.as_uri()])
            offline_index = "no-index = true"
            offline_links = "find-links = " + json.dumps([self.wheels.as_uri()])
            self.assertEqual(text.splitlines().count(offline_index), 1)
            self.assertEqual(text.splitlines().count(offline_links), 1)
            receipt.write_text(
                "\n".join(
                    "index-url = " + json.dumps(updates.INDEX)
                    if line == offline_index
                    else line
                    for line in text.splitlines()
                    if line != offline_links
                )
                + "\n"
            )
            normalized = tomllib.loads(receipt.read_text())
            normalized_options = normalized["tool"]["options"]
            self.assertEqual(normalized_options.pop("index-url"), updates.INDEX)
            normalized_options["no-index"] = True
            normalized_options["find-links"] = [self.wheels.as_uri()]
            self.assertEqual(normalized, before)

    def exercise(self, prefix, *, kind):
        python = prefix / "bin/python"
        package = Path(
            self.command(
                [python, "-I", "-c", "import oc_box; print(oc_box.__path__[0])"]
            )
        )
        site = package.parent
        observed_versions = []

        def distribution(name):
            self.assertEqual(name, "oc-box")
            # Each invocation models a new launcher process, not a Distribution
            # still pointing at the dist-info removed by the previous manager.
            dist = next(
                d
                for d in metadata.distributions(path=[str(site)])
                if d.metadata["Name"] == name
            )
            observed_versions.append(dist.version)
            return dist

        original = updates._run
        mutations = []

        def offline(argv, work, env, **kwargs):
            argv = list(map(str, argv))
            if kind == "pipx" and "install" in argv:
                mutations.append(argv[:])
                self.assertIn("--pip-args", argv)
                position = argv.index("--pip-args") + 1
                flags = shlex.split(argv[position])
                self.assertEqual(flags, updates.PIP_FLAGS)
                index = flags.index("--index-url")
                flags[index : index + 2] = [
                    "--no-index",
                    "--find-links",
                    str(self.wheels),
                ]
                argv[position] = shlex.join(flags)
            elif (kind == "pip" and "install" in argv) or (
                kind == "uv" and "install" in argv
            ):
                self.assertIn(updates.INDEX, argv)
                mutations.append(argv[:])
                index = argv.index(
                    "--index-url" if kind == "pip" else "--default-index"
                )
                argv[index : index + 2] = [
                    "--no-index",
                    "--find-links",
                    str(self.wheels),
                ]
            return original(argv, work, env, **kwargs)

        patches = [
            patch.object(updates.os, "geteuid", return_value=1000),
            patch.object(updates.sys, "prefix", str(prefix)),
            patch.object(updates.sys, "base_prefix", sys.base_prefix),
            patch.object(updates, "_private", return_value=True),
            patch.object(
                updates,
                "_operation_lock",
                side_effect=lambda *a: contextlib.nullcontext(),
            ),
            patch.object(updates.metadata, "distribution", side_effect=distribution),
            patch.object(updates, "_run", side_effect=offline),
            patch.object(channels, "fetch_json", side_effect=self.discovery),
            patch.dict(
                os.environ,
                {"OC_BOX_CONTAINER": "", "XDG_CONFIG_HOME": str(self.work / "config")},
            ),
        ]
        with contextlib.ExitStack() as stack:
            for condition in patches:
                stack.enter_context(condition)
            for channel, version, count in (
                (None, "1.6.2", 1),
                ("devel", "1.6.3.dev0", 2),
                (None, "1.6.3.dev0", 2),
                ("stable", "1.6.2", 3),
            ):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertIsNone(updates.upgrade(package, channel=channel))
                self.assertEqual(len(mutations), count)
                fresh = json.loads(
                    original([python, "-I", "-c", updates.PROBE], self.work, self.env)
                )
                self.assertEqual(fresh["version"], version)
                self.assertEqual(fresh["prefix"], str(prefix))
                self.assertFalse(fresh["direct"])
                selected = "devel" if version.endswith(".dev0") else "stable"
                self.assertIn(f"PyPI {selected}: {version}", output.getvalue())
                if channel == "stable":
                    self.assertIn(
                        "workspace/session state is not rolled back", output.getvalue()
                    )
                settings = json.loads(
                    (self.work / "config/oc-box/config.json").read_text()
                )
                self.assertEqual(
                    list(settings["package_update_channels"].values()), [selected]
                )
                if channel is None and selected == "devel":
                    self.assertIn("up to date", output.getvalue())
                    if kind in ("pipx", "uv"):
                        self.assertEqual(receipt.read_bytes(), previous_receipt)
                else:
                    self.assertIn("oc-box==" + version, mutations[-1])
                    self.normalize_receipt_transport(prefix, kind)
                    if kind == "pipx":
                        flags = json.loads((prefix / "pipx_metadata.json").read_text())[
                            "main_package"
                        ]["pip_args"]
                        self.assertEqual(
                            flags, ["--force-reinstall", *updates.PIP_FLAGS]
                        )
                    elif kind == "uv":
                        import tomllib

                        options = tomllib.loads(
                            (prefix / "uv-receipt.toml").read_text()
                        )["tool"]["options"]
                        self.assertEqual(options["keyring-provider"], "disabled")
                        self.assertIs(options["no-build"], True)
                if kind in ("pipx", "uv"):
                    receipt = prefix / (
                        "pipx_metadata.json" if kind == "pipx" else "uv-receipt.toml"
                    )
                    previous_receipt = receipt.read_bytes()
        self.assertEqual(
            observed_versions,
            ["1.6.1", "1.6.2", "1.6.3.dev0", "1.6.3.dev0"],
        )
        return package, fresh

    def test_real_private_venv_pip_channel_roundtrip_checks_fresh_metadata(self):
        prefix = self.work / "private"
        self.command([sys.executable, "-m", "venv", str(prefix)])
        self.command(
            [
                prefix / "bin/python",
                "-I",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--no-index",
                "--find-links",
                self.wheels,
                "oc-box==1.6.1",
            ]
        )
        self.exercise(prefix, kind="pip")

    @unittest.skipUnless(
        shutil.which("pipx"), "An existing pipx is needed for its native fixture."
    )
    def test_real_pipx_legacy_receipt_channel_roundtrip(self):
        executable = Path(shutil.which("pipx")).resolve()
        home = self.work / "pipx"
        self.env.update(
            {
                "PIPX_HOME": str(home),
                "PIPX_BIN_DIR": str(self.work / "bin"),
                "PIPX_MAN_DIR": str(self.work / "man"),
            }
        )
        # A local ensurepip seed keeps pipx's shared-library setup offline.
        self.command([sys.executable, "-m", "venv", home / "shared"])
        self.command(
            [
                executable,
                "install",
                "--python",
                sys.executable,
                "--pip-args",
                shlex.join(
                    ["--no-index", "--find-links", str(self.wheels)]
                    + updates.PIP_FLAGS[2:]
                ),
                "oc-box==1.6.1",
            ]
        )
        prefix = home / "venvs/oc-box"
        receipt = prefix / "pipx_metadata.json"
        data = json.loads(receipt.read_text())
        if data.get("pipx_metadata_version") not in ("0.4", "0.5"):
            self.skipTest(
                "This pipx receipt version is deliberately unsupported; use pipx 1.8 for the mutation fixture."
            )
        self.normalize_receipt_transport(prefix, "pipx")
        with patch.dict(
            os.environ,
            {
                "PIPX_BIN_DIR": self.env["PIPX_BIN_DIR"],
                "PIPX_MAN_DIR": self.env["PIPX_MAN_DIR"],
            },
        ):
            self.exercise(prefix, kind="pipx")
        self.assertEqual(
            json.loads(receipt.read_text())["main_package"]["package_version"], "1.6.2"
        )

    @unittest.skipIf(
        sys.version_info < (3, 11), "uv receipt parsing needs stdlib tomllib"
    )
    def test_real_uv_owner_receipt_launcher_channel_roundtrip(self):
        self.command(
            [
                self.uv,
                "tool",
                "install",
                "--no-config",
                "--no-index",
                "--find-links",
                self.wheels,
                "oc-box==1.6.1",
            ]
        )
        prefix = self.work / "tools/oc-box"
        receipt = prefix / "uv-receipt.toml"
        self.normalize_receipt_transport(prefix, "uv")
        with patch.object(updates.shutil, "which", return_value=str(self.uv)):
            _, fresh = self.exercise(prefix, kind="uv")
        self.assertEqual(fresh["version"], "1.6.2")
        self.assertIn('specifier = "==1.6.2"', receipt.read_text())
        self.assertTrue((self.work / "bin/oc-box").is_symlink())


if __name__ == "__main__":
    unittest.main()
