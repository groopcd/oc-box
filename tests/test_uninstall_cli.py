import io
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from oc_box import (
    install_ownership,
    install_removal,
    installation,
    oc_box,
    uninstall_cli,
    uninstall_plan,
    uninstall_review,
    workspace_uninstall,
)

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
        self.assertIn(
            "currently refuses without removing anything",
            " ".join(self.out.getvalue().split()),
        )
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

    def test_full_refuses_both_modes_and_guides_separate_launcher_only_preview(self):
        for args in (["--full"], ["--full", "--dry-run"]):
            with (
                self.subTest(args=args),
                redirect_stdout(io.StringIO()) as output,
                patch.object(oc_box, "PACKAGE", RELEASE),
                patch.object(oc_box.sys, "argv", ["oc-box", "--uninstall", *args]),
                patch.object(oc_box, "state_root", side_effect=AssertionError),
                patch.object(oc_box, "podman", side_effect=AssertionError),
                patch.object(oc_box, "sync_sessions", side_effect=AssertionError),
                patch.object(
                    workspace_uninstall, "plan_workspaces", side_effect=AssertionError
                ),
                patch.object(
                    uninstall_plan.UninstallSnapshot,
                    "render",
                    side_effect=AssertionError,
                ),
                patch.object(subprocess, "Popen", side_effect=AssertionError),
            ):
                self.assertEqual(oc_box.entrypoint(), 1)
                report = output.getvalue()
                self.assertIn("Workspace engine ownership was not queried", report)
                self.assertIn(
                    "Podman cleanup and oc-box state removal were not verified", report
                )
                self.assertIn("Nothing was removed", report)
                self.assertIn("workspace containers and home volumes", report)
                self.assertIn(".oc-box-id markers, external backups", report)
                self.assertIn("host OpenCode history were not touched", report)
                self.assertIn(
                    "Leave ambiguous, shared, or unrelated resources untouched",
                    " ".join(report.split()),
                )
                self.assertLess(
                    report.index("Keep the launcher installed"),
                    report.index("`oc-box --uninstall --dry-run`"),
                )
                self.assertIn("`oc-box list`", report)
                self.assertIn("`oc-box --uninstall` without `--full`", report)
                self.assertNotIn("Proposed removal", report)
                self.assertNotIn("Required confirmation", report)
        self.approve.assert_not_called()
        self.remove.assert_not_called()

    def test_full_cannot_confirm_or_remove_even_if_review_returns_no_blockers(self):
        snapshot = uninstall_plan.UninstallSnapshot(
            uninstall_review.UninstallRequest(full=True, dry_run=False),
            INSTALLATION,
            None,
            (),
            (),
        )
        with patch.object(uninstall_plan, "prepare_review", return_value=snapshot):
            self.assertEqual(self.invoke("--full"), 1)
        self.assertIn("Nothing was removed", self.out.getvalue())
        self.approve.assert_not_called()
        self.remove.assert_not_called()

    def test_package_guidance_is_separate_from_full_and_uses_verified_manager(self):
        for manager, command in (
            ("pipx", "pipx uninstall oc-box"),
            ("uv", "uv tool uninstall oc-box"),
        ):
            self.review.return_value = install_ownership.PackageInstallReview(
                manager, Path("/synthetic/owned-env"), "1.9.427.dev0"
            )
            for args in ([], ["--full"], ["--full", "--dry-run"]):
                with (
                    self.subTest(manager=manager, args=args),
                    redirect_stdout(io.StringIO()) as output,
                ):
                    self.assertEqual(self.invoke(*args), 1)
                    report = output.getvalue()
                    self.assertIn("No verified automatic removal", report)
                    self.assertIn(command, report)
                    self.assertNotIn("`oc-box --uninstall --dry-run`", report)
                    self.assertNotIn("Required confirmation", report)
                    if args:
                        self.assertIn("Nothing was removed", report)
                        self.assertLess(
                            report.index("Keep the launcher installed"),
                            report.index(command),
                        )
        self.approve.assert_not_called()
        self.remove.assert_not_called()

    def test_full_unverified_installation_does_not_infer_package_manager(self):
        self.review.side_effect = install_ownership.OwnershipError()
        self.assertEqual(self.invoke("--full"), 1)
        report = self.out.getvalue()
        self.assertIn("could not verify the running installation", report)
        self.assertIn("Identify your installation first", report)
        self.assertIn("verified active GitHub installation", report)
        self.assertNotIn("pipx uninstall", report)
        self.assertNotIn("uv tool uninstall", report)
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


class InstalledUninstallHelpTests(unittest.TestCase):
    def test_flat_install_can_load_its_own_dedicated_uninstall_help(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with (
                patch.object(installation.shutil, "which", return_value=None),
                redirect_stdout(io.StringIO()),
            ):
                installation.install(source, home)
            result = subprocess.run(
                [str(home / ".local/bin/oc-box"), "--uninstall", "--help"],
                env={"HOME": directory, "PATH": "/usr/bin:/bin"},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertIn(b"oc-box --uninstall", result.stdout)
        self.assertEqual(result.stderr, b"")
