"""Offline exact-profile guards; no native or Podman acceptance is claimed."""

from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import native_host_profile as profile


class NativeHostProfileTests(unittest.TestCase):
    def test_exact_profiles_preserve_the_legacy_default_and_private_probe(self):
        root, binary = Path("/synthetic/fixture"), Path("/synthetic/opencode")
        for expected in profile.NATIVE_VERSIONS:
            with (
                self.subTest(expected=expected),
                patch.object(
                    profile, "native_env", return_value={"PRIVATE": "synthetic"}
                ) as env,
                patch.object(
                    profile.box,
                    "run",
                    return_value=SimpleNamespace(stdout=expected + "\n"),
                ) as run,
            ):
                self.assertEqual(
                    profile.check_native_version(root, binary, "2", expected=expected),
                    expected,
                )
                env.assert_called_once_with(root, "2")
                run.assert_called_once_with(
                    [str(binary), "--version"],
                    env={"PRIVATE": "synthetic"},
                    stdin=subprocess.DEVNULL,
                    capture=True,
                    private=True,
                    timeout=15,
                )
        self.assertEqual(profile.native_version(), "2.2.6")

    def test_unknown_profile_and_wrong_major_refuse_before_native_execution(self):
        for expected, major in (
            ("2", "2"),
            ("2.2.7", "2"),
            ("2.3.1", "2"),
            ("2.3.0", "1"),
        ):
            with (
                self.subTest(expected=expected, major=major),
                patch.object(profile.box, "run") as run,
                self.assertRaises(ValueError),
            ):
                profile.check_native_version(
                    Path("/fixture"), "/native", major, expected=expected
                )
            run.assert_not_called()

    def test_mismatch_and_malformed_output_never_relax_to_any_major_two(self):
        for expected in profile.NATIVE_VERSIONS:
            for output in (
                "2.2.7",
                "2.3.1",
                "private-invalid-output",
                *profile.NATIVE_VERSIONS,
            ):
                if output == expected:
                    continue
                with (
                    self.subTest(expected=expected, output=output),
                    patch.object(profile, "native_env", return_value={}),
                    patch.object(
                        profile.box, "run", return_value=SimpleNamespace(stdout=output)
                    ),
                    self.assertRaises(ValueError) as raised,
                ):
                    profile.check_native_version(
                        Path("/fixture"), "/native", "2", expected=expected
                    )
                self.assertNotIn(output, str(raised.exception))

    def test_identity_binds_selected_version_real_path_and_binary_bytes(self):
        with tempfile.TemporaryDirectory(prefix="native-profile-unit-") as directory:
            binary = Path(directory) / "native"
            binary.write_bytes(b"synthetic-original-native")
            original = profile.native_identity(binary, "2.3.0")
            self.assertEqual(original["path"], str(binary.resolve()))
            self.assertEqual(original["version"], "2.3.0")
            binary.write_bytes(b"synthetic-replaced-native")
            self.assertNotEqual(
                profile.native_identity(binary, "2.3.0")["sha256"], original["sha256"]
            )
