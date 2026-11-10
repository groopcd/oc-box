"""Synthetic exec accounting only; no vault, engines, or native services."""

import copy
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
