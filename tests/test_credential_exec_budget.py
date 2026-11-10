"""Synthetic exec accounting only; no vault, engines, or native services."""

import copy
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from oc_box import credentials, vault


class CredentialExecBudgetTests(unittest.TestCase):
    def setUp(self):
        preferred = Path("/tmp/opencode")
        temporary = tempfile.TemporaryDirectory(
            prefix="oc-box-exec-budget-", dir=preferred if preferred.is_dir() else None
        )
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin:/bin",
                "HOME": str(self.root / "host"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "OC_BOX_CONTAINER": "0",
            },
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.data = {
            "id": "a" * 32,
            "home": str(self.root / "container"),
            "project": str(self.root / "project"),
            "container": "oc-box-synthetic",
            "opencode_version": "2.2.6",
        }

    def assert_refused(self, values, *, limit, data=None):
        data = self.data if data is None else data
        before = copy.deepcopy(data)
        environment = dict(os.environ)
        with (
            patch.object(vault, "is_present", return_value=True),
            patch.object(vault, "select_all", return_value=values),
            patch.object(vault, "select", return_value=values),
            patch.object(
                credentials.os,
                "sysconf",
                side_effect=lambda name: {"SC_ARG_MAX": limit, "SC_PAGESIZE": 4096}[
                    name
                ],
            ),
            self.assertRaisesRegex(
                vault.VaultError, "exec.*budget|exec.*limit"
            ) as error,
        ):
            with credentials.unlocked(data):
                self.fail("Exec preflight must not yield an owning launch")
        self.assertNotIn("synthetic-private", str(error.exception))
        self.assertEqual(values, {})
        self.assertEqual(data, before)
        self.assertEqual(dict(os.environ), environment)
        self.assertIsNone(credentials._UNLOCKED.get())
        self.assertIsNone(credentials._EFFECTIVE_POLICY.get())

    def test_effective_low_argmax_refuses_before_launch_and_clears_cache(self):
        self.assert_refused({"FIXTURE_TOKEN": "synthetic-private-value"}, limit=131072)

    def test_unknown_or_invalid_limit_refuses_without_disclosing_values(self):
        for limit in (-1, 0, True, "synthetic-private-unavailable"):
            with self.subTest(limit=limit):
                self.assert_refused(
                    {"FIXTURE_TOKEN": "synthetic-private-value"}, limit=limit
                )

    def test_actual_ambient_environment_consumes_headroom_even_in_global_mode(self):
        for index in range(3):
            os.environ[f"AMBIENT_{index}"] = "synthetic-private-" + "x" * 60000
        self.assert_refused({"FIXTURE_TOKEN": "v" * 60000}, limit=300000)

    def test_per_string_bound_applies_to_inherited_environment_and_actual_argv(self):
        with patch.dict(
            os.environ, {"AMBIENT_SECRET": "synthetic-private-" + "x" * 131072}
        ):
            self.assert_refused({"FIXTURE_TOKEN": "value"}, limit=2097152)
        data = {**self.data, "home": "/synthetic/" + "x" * 131072}
        self.assert_refused({"FIXTURE_TOKEN": "value"}, limit=2097152, data=data)

    def test_named_vault_selection_also_respects_actual_exec_capacity(self):
        for version in ("1.19.5", "2.2.6"):
            data = {
                **self.data,
                "opencode_version": version,
                "credentials": {
                    "mode": "selected",
                    "env": [],
                    "vault": ["FIXTURE_TOKEN"],
                },
            }
            with self.subTest(version=version):
                self.assert_refused(
                    {"FIXTURE_TOKEN": "synthetic-private"}, limit=131072, data=data
                )

    def test_pointer_storage_counts_many_short_ambient_entries(self):
        environment = {f"E{index}": "x" for index in range(10000)}
        with patch.dict(os.environ, environment):
            self.assert_refused({"FIXTURE_TOKEN": "value"}, limit=280000)

    def test_reasonable_budget_preserves_exact_unicode_values_and_host_environment(
        self,
    ):
        values = {"FIXTURE_TOKEN": "synthetic-private-é" * 100}
        environment = dict(os.environ)
        with (
            patch.object(vault, "is_present", return_value=True),
            patch.object(vault, "select_all", return_value=values),
            patch.object(
                credentials.os,
                "sysconf",
                side_effect=lambda name: {"SC_ARG_MAX": 300000, "SC_PAGESIZE": 4096}[
                    name
                ],
            ),
        ):
            with credentials.unlocked(self.data):
                self.assertEqual(
                    credentials.active_values(self.data)["FIXTURE_TOKEN"],
                    "synthetic-private-é" * 100,
                )
                self.assertEqual(dict(os.environ), environment)
        self.assertEqual(values, {})

    def test_inherited_filesystem_encoded_bytes_need_not_be_valid_utf8(self):
        values = {"FIXTURE_TOKEN": "synthetic-private-é"}
        with (
            patch.dict(os.environ, {"AMBIENT_BYTES": "\udcff" * 60000}),
            patch.object(vault, "is_present", return_value=True),
            patch.object(vault, "select_all", return_value=values),
        ):
            with credentials.unlocked(self.data):
                self.assertEqual(
                    credentials.active_values(self.data)["FIXTURE_TOKEN"],
                    "synthetic-private-é",
                )
        self.assertEqual(values, {})

    def test_low_stack_child_demonstrates_e2big_and_preflight_without_parent_changes(
        self,
    ):
        # Only this bounded, synthetic child changes its own RLIMIT_STACK.
        # No shell, OpenCode, engines, real credentials or host state are used.
        program = """import errno,json,os,resource,subprocess
from unittest.mock import patch
from oc_box import credentials,vault
_,hard=resource.getrlimit(resource.RLIMIT_STACK)
resource.setrlimit(resource.RLIMIT_STACK,(min(524288,hard) if hard != resource.RLIM_INFINITY else 524288,hard))
values={f'FIXTURE_{i}_TOKEN':'x'*65536 for i in range(3)}
try:
    subprocess.run(['/bin/true'],env=values,check=True,timeout=5)
except OSError as error:
    assert error.errno == errno.E2BIG
else:
    raise AssertionError('Expected finite low-stack exec refusal')
data=json.loads(os.environ['FIXTURE_DATA'])
with patch.object(vault,'is_present',return_value=True),patch.object(vault,'select_all',return_value=values):
    try:
        with credentials.unlocked(data):
            raise AssertionError('Preflight unexpectedly yielded')
    except vault.VaultError as error:
        assert 'exec budget/limit' in str(error)
assert values == {} and credentials._UNLOCKED.get() is None
assert credentials._EFFECTIVE_POLICY.get() is None
print(json.dumps({'exec_refused':True,'preflight_refused':True}))
"""
        original_stack = resource.getrlimit(resource.RLIMIT_STACK)
        result = subprocess.run(
            [sys.executable, "-B", "-c", program],
            env={**os.environ, "FIXTURE_DATA": json.dumps(self.data)},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(resource.getrlimit(resource.RLIMIT_STACK), original_stack)
        self.assertEqual(
            json.loads(result.stdout), {"exec_refused": True, "preflight_refused": True}
        )


class FlatLauncherBudgetBoundaryTests(unittest.TestCase):
    def run_fixture(self, mode):
        preferred = Path("/tmp/opencode")
        with tempfile.TemporaryDirectory(
            prefix="oc-box-flat-budget-", dir=preferred if preferred.is_dir() else None
        ) as temporary:
            root = Path(temporary)
            release = root / "release"
            shutil.copytree(
                Path(credentials.__file__).parent,
                release,
                ignore=shutil.ignore_patterns("__pycache__", "vault-deps"),
            )
            config = root / "config/oc-box/config.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"pass_env": [123] if mode == "main" else []}))
            environment = {
                "PATH": "/usr/bin:/bin",
                "HOME": str(root / "home"),
                "OC_BOX_CONTAINER": "0",
                **{
                    "XDG_" + name + "_HOME": str(root / name.lower())
                    for name in ("CONFIG", "DATA", "CACHE", "STATE")
                },
            }
            program = r"""import importlib.util,json,sys,types
from pathlib import Path
release=Path(sys.argv[1])
sys.path.insert(0,str(release))
def tripwire(event,args):
    if event in ('subprocess.Popen','os.system','os.exec','os.posix_spawn','os.fork','os.forkpty'):
        raise AssertionError('No fixture command execution is permitted')
sys.addaudithook(tripwire)
data={'id':'a'*32,'home':str(release/'home'),'project':str(release/'project'),
      'container':'synthetic-no-container','opencode_version':'2.2.6',
      'credentials':{'mode':'selected','env':[],'vault':['FIXTURE_TOKEN']}}
values={'FIXTURE_TOKEN':'synthetic-private-flat-value'}
if sys.argv[2] == 'main':
    spec=importlib.util.spec_from_file_location('__main__',release/'oc_box.py')
    box=importlib.util.module_from_spec(spec)
    sys.modules['__main__']=box
    observed=[]
    def owner():
        with box.credentials.unlocked(data):
            raise AssertionError('Invalid pass_env must not yield')
    def trace(frame,event,arg):
        if event == 'call' and frame.f_code.co_name == 'entrypoint' and Path(frame.f_code.co_filename) == release/'oc_box.py':
            sys.settrace(None)
            box.credentials.vault.select=lambda names:values
            original=box.exec_command
            def observed_command(*args,**kwargs):
                observed.append('exec')
                return original(*args,**kwargs)
            box.exec_command=observed_command
            box.main=owner
        return trace
    sys.settrace(trace)
    try:
        spec.loader.exec_module(box)
    except SystemExit as error:
        assert error.code == 1
    else:
        raise AssertionError('Expected native launcher error boundary')
    assert 'oc_box' not in sys.modules, 'A second app module was loaded'
    assert observed == ['exec'], 'The running launcher hook was not reused'
    c=box.credentials
else:
    import credentials as c
    unrelated=sys.modules['__main__']
    unrelated.__file__=str(release/'unrelated.py')
    unrelated.credentials=c
    unrelated.PACKAGE=release
    def reject(*args,**kwargs):
        raise AssertionError('Unrelated __main__ must never supply the app facade')
    unrelated.exec_command=reject
    unrelated.container_env=reject
    c.vault.select=lambda names:values
    with c.unlocked(data):
        assert c.active_values(data)['FIXTURE_TOKEN']=='synthetic-private-flat-value'
    box=sys.modules['oc_box']
    assert box is not unrelated and box.credentials is c
    assert Path(box.__file__) == release/'oc_box.py'
assert values == {} and c._UNLOCKED.get() is None and c._EFFECTIVE_POLICY.get() is None
print(json.dumps({'boundary_ok':True,'cache_clear':True}))
"""
            result = subprocess.run(
                [sys.executable, "-B", "-c", program, str(release), mode],
                cwd=root,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(
                "synthetic-private-flat-value", result.stdout + result.stderr
            )
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(
                json.loads(result.stdout), {"boundary_ok": True, "cache_clear": True}
            )
            if mode == "main":
                self.assertIn("oc-box: Invalid pass_env entry", result.stderr)

    def test_flat_running_launcher_preserves_its_module_identity_and_error_boundary(
        self,
    ):
        self.run_fixture("main")

    def test_flat_library_fallback_ignores_unrelated_main(self):
        self.run_fixture("library")

    def test_package_preflight_uses_canonical_app_without_executing_commands(self):
        from oc_box import oc_box as box

        unrelated = types.ModuleType("__main__")
        unrelated.__file__ = box.__file__
        unrelated.PACKAGE = box.PACKAGE
        unrelated.credentials = object()
        preferred = Path("/tmp/opencode")
        with (
            tempfile.TemporaryDirectory(
                prefix="oc-box-package-budget-",
                dir=preferred if preferred.is_dir() else None,
            ) as temporary,
            patch.dict(
                os.environ,
                {
                    "PATH": "/usr/bin:/bin",
                    "HOME": temporary,
                    "XDG_CONFIG_HOME": temporary,
                    "OC_BOX_CONTAINER": "0",
                },
                clear=True,
            ),
            patch.dict(sys.modules, {"__main__": unrelated}),
            patch.object(
                box.subprocess,
                "run",
                side_effect=AssertionError("No command execution"),
            ),
            patch.object(
                box.subprocess,
                "Popen",
                side_effect=AssertionError("No command execution"),
            ),
            patch.object(box, "exec_command", wraps=box.exec_command) as builder,
        ):
            data = {
                "id": "a" * 32,
                "home": temporary,
                "project": temporary,
                "container": "synthetic-no-container",
                "opencode_version": "2.2.6",
            }
            values = {"FIXTURE_TOKEN": "synthetic-private-package"}
            with (
                patch.object(vault, "is_present", return_value=True),
                patch.object(vault, "select_all", return_value=values),
            ):
                with credentials.unlocked(data):
                    self.assertTrue(credentials.is_active(data))
            self.assertEqual(values, {})
            builder.assert_called_once()


if __name__ == "__main__":
    unittest.main()
