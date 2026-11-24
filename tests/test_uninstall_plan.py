import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from oc_box import (
    install_ownership,
    uninstall_plan,
    uninstall_review,
    workspace_uninstall,
)


RELEASE = Path("/synthetic/share/oc-box/releases/synthetic")
LAUNCHER = Path("/synthetic/bin/oc-box")
INSTALLATION = install_ownership.FlatInstallPlan(
    RELEASE,
    (
        install_ownership.InstallArtifact(RELEASE / "oc_box.py", (), ()),
        install_ownership.InstallArtifact(LAUNCHER, (), ()),
        install_ownership.InstallArtifact(Path("/synthetic/man/man1/oc-box.1"), (), ()),
    ),
    (),
)


class PrepareReviewTests(unittest.TestCase):
    def plan(self, argv, workspace_review, *, capture=INSTALLATION, failure=None):
        with patch.object(
            install_ownership,
            "review_installation",
            return_value=capture,
            side_effect=failure,
        ) as review:
            request = uninstall_review.parse(argv)
            result = uninstall_plan.prepare_review(
                request, RELEASE, workspace_review=workspace_review
            )
            review.assert_called_once_with(RELEASE, home=None)
            return result

    def test_default_and_full_dry_run_never_query_the_workspace_engine(self):
        workspace = Mock(side_effect=AssertionError)
        default = self.plan(["--uninstall"], workspace)
        self.assertIs(default.installation, INSTALLATION)
        self.assertIsNone(default.workspace)
        self.assertFalse(default.blockers)
        self.assertIn(
            "All oc-box data and Podman workspaces are kept", default.render()
        )
        self.assertIn(str(RELEASE), default.render())
        self.assertNotIn(str(RELEASE / "oc_box.py"), default.render())
        preview = self.plan(["--uninstall", "--dry-run", "--full"], workspace)
        self.assertIn("not queried in this dry run", preview.render())
        self.assertNotIn("Required confirmation", preview.render())
        workspace.assert_not_called()

    def test_unverified_installation_refuses_before_workspace_review(self):
        workspace = Mock(side_effect=AssertionError)
        for capture, failure in (
            (None, install_ownership.OwnershipError()),
            (
                install_ownership.PackageInstallReview("uv", RELEASE, "1.9.427.dev0"),
                None,
            ),
        ):
            with self.subTest(capture=type(capture)):
                outcome = self.plan(
                    ["--uninstall", "--full"],
                    workspace,
                    capture=capture,
                    failure=failure,
                )
                self.assertIsNone(outcome.installation)
                self.assertFalse(outcome.items)
                self.assertNotIn("Required confirmation", outcome.render())
        workspace.assert_not_called()

    def test_full_scope_only_projects_verified_objects_and_never_adopts_state_paths(
        self,
    ):
        targets = workspace_uninstall.WorkspaceTargets(
            registration=None,
            container=SimpleNamespace(name="oc-box-synthetic", ident="a" * 64),
            home_volume=SimpleNamespace(name="oc-box-home-synthetic"),
        )
        verified = workspace_uninstall.WorkspacePlan(
            state_root="/synthetic/state",
            registry_chain=(),
            registry_present=True,
            engine=None,
            workspaces=(targets,),
            retained_networks=("foreign-network",),
        )
        scoped = Mock(return_value=verified)
        outcome = self.plan(["--uninstall", "--full"], scoped)
        scoped.assert_called_once_with()
        self.assertIs(outcome.workspace, verified)
        self.assertIn("oc-box-home-synthetic", outcome.render())
        self.assertIn("a" * 64, outcome.render())
        self.assertNotIn("foreign-network", outcome.render())
        self.assertNotIn("/synthetic/state", outcome.render())

    def test_full_scope_refusal_keeps_installation_and_disables_confirmation(self):
        scoped = Mock(return_value=None)
        refused = self.plan(["--uninstall", "--full"], scoped)
        self.assertTrue(refused.blockers)
        self.assertIs(refused.installation, INSTALLATION)
        self.assertNotIn("Required confirmation", refused.render())
