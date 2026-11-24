import io
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from oc_box import install_ownership, install_removal, oc_box, uninstall_cli

from test_uninstall_plan import INSTALLATION, RELEASE


class UninstallCLITests(unittest.TestCase):
    def setUp(self):
        self.out, self.err = io.StringIO(), io.StringIO()
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        contexts.enter_context(redirect_stdout(self.out))
        contexts.enter_context(redirect_stderr(self.err))
        self.guard = contexts.enter_context(
            patch.object(install_removal, "_guard_host")
        )
        self.review = contexts.enter_context(
            patch.object(
                install_ownership, "review_installation", return_value=INSTALLATION
            )
        )
        self.approve = contexts.enter_context(
            patch.object(uninstall_cli.uninstall_tty, "confirm", return_value=True)
        )
        self.remove = contexts.enter_context(
            patch.object(
                install_removal,
                "remove_flat_installation",
                return_value=len(INSTALLATION.artifacts),
            )
        )

    def invoke(self, *args):
        return uninstall_cli.cli(RELEASE, ["--uninstall", *args])

    def test_help_before_host_checks_or_locks(self):
        with self.assertRaises(SystemExit) as result:
            self.invoke("--help")
        self.assertEqual(result.exception.code, 0)
        self.assertIn("oc-box --uninstall", self.out.getvalue())
        self.guard.assert_not_called()
        self.review.assert_not_called()
        self.approve.assert_not_called()
        self.remove.assert_not_called()

    def test_entrypoint_bypasses_default_launch_parser(self):
        with (
            patch.object(oc_box, "PACKAGE", RELEASE),
            patch.object(oc_box.sys, "argv", ["oc-box", "--uninstall", "--dry-run"]),
            patch.object(oc_box, "parser", side_effect=AssertionError),
            patch.object(oc_box, "state_root", side_effect=AssertionError),
            patch.object(oc_box, "podman", side_effect=AssertionError),
        ):
            self.assertEqual(oc_box.entrypoint(), 0)
        self.review.assert_called_once_with(RELEASE, home=None)
        self.assertIn("Dry run; nothing was removed.", self.out.getvalue())
        self.approve.assert_not_called()
        self.remove.assert_not_called()

    def test_exact_review_requires_confirmation_before_removal(self):
        def verify_review(snapshot):
            self.assertIs(snapshot.installation, INSTALLATION)
            self.assertIn(str(RELEASE), snapshot.render())
            self.remove.assert_not_called()
            return True

        self.approve.side_effect = verify_review
        self.assertEqual(self.invoke(), 0)
        self.remove.assert_called_once_with(INSTALLATION, runtime=RELEASE)
        self.assertIn("host OpenCode history were kept", self.out.getvalue())

    def test_missing_approval_and_unavailable_host_are_safe_refusals(self):
        self.approve.return_value = False
        self.assertEqual(self.invoke(), 1)
        self.assertIn("nothing was removed", self.err.getvalue())
        self.remove.assert_not_called()
        self.review.reset_mock()
        self.approve.reset_mock()
        self.guard.side_effect = install_removal.InstallRemovalError(
            "Synthetic host refusal."
        )
        self.assertEqual(self.invoke(), 1)
        self.review.assert_not_called()
        self.approve.assert_not_called()
        self.remove.assert_not_called()

    def test_incomplete_full_and_package_never_run_a_narrower_deletion(self):
        self.assertEqual(self.invoke("--full"), 1)
        self.assertEqual(self.invoke("--full", "--dry-run"), 1)
        self.assertIn("Workspace engine ownership was not queried", self.out.getvalue())
        self.review.return_value = install_ownership.PackageInstallReview(
            "pipx", Path("/synthetic/owned-env"), "1.9.427.dev0"
        )
        self.assertEqual(self.invoke(), 1)
        self.assertIn("No verified automatic removal", self.out.getvalue())
        self.approve.assert_not_called()
        self.remove.assert_not_called()

    def test_partial_removal_has_fixed_failure_and_reports_retained_staging(self):
        self.remove.side_effect = install_removal.InstallRemovalError(
            "Synthetic unconfirmed completion.",
            changed=True,
            retained=Path("/synthetic/staging-\n"),
        )
        self.assertEqual(self.invoke(), 1)
        self.remove.assert_called_once()
        self.assertIn('"/synthetic/staging-\\n"', self.err.getvalue())
        self.assertIn("unconfirmed completion", self.err.getvalue())
        self.assertNotIn("launcher removed", self.out.getvalue())
