"""Private capture consumers share the real Linux compatibility path."""

import contextlib
import errno
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from oc_box import backup_crypto, credentials, dependencies, oc_box as box


@unittest.skipUnless(sys.platform == "linux", "Linux anonymous memory files required")
class PrivateOutputTests(unittest.TestCase):
    def test_no_vault_private_subprocess_capture_works_without_python_bindings(self):
        with (
            patch.dict(os.__dict__),
            patch.object(
                tempfile, "TemporaryFile", side_effect=AssertionError("no disk capture")
            ),
            credentials.unlocked(
                {"id": "synthetic-private-output"}, version="2.2.6", no_vault=True
            ),
        ):
            os.__dict__.pop("memfd_create", None)
            os.__dict__.pop("MFD_CLOEXEC", None)
            self.assertFalse(credentials.has_unlocked_values())
            box.check_private_output()
            result = box.run(
                [
                    sys.executable,
                    "-c",
                    "import os; print(os.readlink('/proc/self/fd/1')); print('synthetic-private')",
                ],
                capture=True,
                private=True,
            )
            self.assertIn("memfd:oc-box-private-output", result.stdout)
            self.assertIn("synthetic-private", result.stdout)
            with self.assertRaises(box.BoxError) as error:
                box.run(
                    [
                        sys.executable,
                        "-c",
                        "import sys; print('synthetic-private'); sys.exit(1)",
                    ],
                    capture=True,
                    private=True,
                )
            self.assertNotIn("synthetic-private", str(error.exception))

    def test_backup_keys_and_doctor_use_the_same_memory_only_fallback(self):
        with (
            patch.dict(os.__dict__),
            patch.object(
                tempfile, "TemporaryFile", side_effect=AssertionError("no disk capture")
            ),
        ):
            os.__dict__.pop("memfd_create", None)
            os.__dict__.pop("MFD_CLOEXEC", None)
            with backup_crypto.memory_file(b"synthetic-private-key") as stream:
                self.assertTrue(stat.S_ISREG(os.fstat(stream.fileno()).st_mode))
                self.assertFalse(os.get_inheritable(stream.fileno()))
                self.assertIn("memfd:", os.readlink(f"/proc/self/fd/{stream.fileno()}"))
                self.assertEqual(stream.read(), b"synthetic-private-key")
            self.assertTrue(dependencies.probe("memfd")[0])
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(
                    box.main(["doctor", "--feature", "private-output", "--json"]), 0
                )
            self.assertTrue(json.loads(output.getvalue())["features"][0]["available"])

    def test_unavailable_capture_never_executes_or_spools_and_reports_interpreter(self):
        with (
            patch.object(
                box.private_memory,
                "create",
                side_effect=OSError(errno.ENOSYS, "synthetic-sensitive-error"),
            ),
            patch.object(box.subprocess, "run") as child,
            patch.object(
                tempfile, "TemporaryFile", side_effect=AssertionError("no disk capture")
            ),
        ):
            for consumer in (
                lambda: box.captured_output(private=True),
                backup_crypto.memory_file,
            ):
                with (
                    self.subTest(consumer=consumer),
                    self.assertRaises(
                        (box.BoxError, backup_crypto.BackupError)
                    ) as error,
                ):
                    with consumer():
                        self.fail("unsupported private capture was accepted")
                self.assertIn(sys.executable, str(error.exception))
                self.assertNotIn("synthetic-sensitive-error", str(error.exception))
            with self.assertRaises(box.BoxError):
                box.run(["synthetic-never-executed"], capture=True, private=True)
            self.assertFalse(dependencies.probe("memfd")[0])
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(
                    box.main(["doctor", "--feature", "private-output", "--json"]), 1
                )
            feature = json.loads(output.getvalue())["features"][0]
            self.assertEqual(feature["missing"], ["memfd"])
            self.assertEqual(feature["install_commands"], [])
            child.assert_not_called()
