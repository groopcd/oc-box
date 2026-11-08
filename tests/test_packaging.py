import contextlib
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from oc_box import oc_box

APP = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_console_entrypoint_preserves_readable_error_and_interrupt_handling(self):
        with (
            patch.object(
                oc_box, "main", side_effect=oc_box.BoxError("fixture failure")
            ),
            contextlib.redirect_stderr(io.StringIO()) as output,
        ):
            self.assertEqual(oc_box.entrypoint(), 1)
        self.assertEqual(output.getvalue(), "oc-box: fixture failure\n")
        with patch.object(oc_box, "main", side_effect=KeyboardInterrupt):
            self.assertEqual(oc_box.entrypoint(), 130)

    def test_package_metadata_and_workflow_keep_both_distribution_channels(self):
        metadata = (APP / "pyproject.toml").read_text()
        self.assertIn('oc-box = "oc_box.oc_box:entrypoint"', metadata)
        self.assertIn('license = "GPL-3.0-or-later"', metadata)
        self.assertIn('requires-python = ">=3.10"', metadata)
        self.assertIn('vault = ["cryptography>=44"]', metadata)
        workflow = (APP / ".github/workflows/publish.yml").read_text()
        self.assertIn("name: pypi", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn("tools/check_package.py dist", workflow)
        self.assertIn("default: false", workflow)
        self.assertNotIn("PYPI_TOKEN", workflow)
        self.assertNotIn("PERSONAL_GITHUB_TOKEN", workflow)
        self.assertIn(
            "https://github.com/groopcd/oc-box.git", (APP / "install.sh").read_text()
        )


if __name__ == "__main__":
    unittest.main()
