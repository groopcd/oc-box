import contextlib
import io
import unittest

from oc_box import uninstall_review


class UninstallReviewTests(unittest.TestCase):
    def test_scoped_pure_options_and_help(self):
        self.assertEqual(
            uninstall_review.parse(["--uninstall"]),
            uninstall_review.UninstallRequest(full=False, dry_run=False),
        )
        self.assertEqual(
            uninstall_review.parse(["--uninstall", "--dry-run", "--full"]),
            uninstall_review.UninstallRequest(full=True, dry_run=True),
        )
        for args in (
            ["start", "--uninstall"],
            ["--uninstall", "--yes"],
            ["--uninstall", "--project", "/example"],
            ["--uninstall", "--fu"],
            ["--uninstall", "--all"],
        ):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    uninstall_review.parse(args)
                self.assertEqual(caught.exception.code, 2)
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            self.assertRaises(SystemExit) as caught,
        ):
            uninstall_review.parse(["--uninstall", "--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn(
            "Host OpenCode history is never removed",
            " ".join(output.getvalue().split()),
        )

    def test_default_review_keeps_every_workspace_and_all_ocbox_data(self):
        request = uninstall_review.parse(["--uninstall"])
        review = uninstall_review.render(
            request, (uninstall_review.ReviewItem("launcher", "/example/bin/oc-box"),)
        )
        self.assertIn("All oc-box data and Podman workspaces are kept.", review)
        self.assertIn("Required confirmation: UNINSTALL OC-BOX\n", review)
        with self.assertRaises(uninstall_review.UninstallReviewError):
            uninstall_review.render(
                request, (uninstall_review.ReviewItem("home-volume", "a" * 32),)
            )

    def test_full_review_warns_about_separate_backups_and_removes_nothing_in_preview(
        self,
    ):
        request = uninstall_review.parse(["--uninstall", "--full", "--dry-run"])
        items = (uninstall_review.ReviewItem("container", "a" * 64),)
        review = uninstall_review.render(request, items)
        self.assertIn("backup --host --output FILE", review)
        self.assertIn("not workspace container storage", review)
        self.assertIn("Dry run; nothing was removed.", review)
        self.assertNotIn("Required confirmation", review)

    def test_blocked_review_cannot_offer_confirmation_or_interpret_control_bytes(self):
        request = uninstall_review.parse(["--uninstall", "--full"])
        items = (uninstall_review.ReviewItem("home-volume", "literal\x1b\nname"),)
        review = uninstall_review.render(request, items, blockers=("unverified scope",))
        self.assertNotIn("\x1b", review)
        self.assertIn(r"\u001b\n", review)
        self.assertIn("Blockers (nothing may be removed)", review)
        self.assertNotIn("Required confirmation", review)
        with self.assertRaises(uninstall_review.UninstallReviewError):
            uninstall_review.render(request, items * 2)
        with self.assertRaises(uninstall_review.UninstallReviewError):
            uninstall_review.render(
                request,
                (uninstall_review.ReviewItem("native-history", "/example/store"),),
            )
