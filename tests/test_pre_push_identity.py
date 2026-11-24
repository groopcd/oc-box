import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


HOOK = Path(__file__).resolve().parents[1] / "tools" / "check_push_identity.py"
APPROVED = "maintainer@example.invalid"
UNAPPROVED = "accidental@example.invalid"


@unittest.skipUnless(shutil.which("git"), "requires Git")
class PrePushIdentityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.repo = root / "source"
        self.remote = root / "destination.git"
        self.repo.mkdir()
        self.env = {
            key: val for key, val in os.environ.items() if not key.startswith("GIT_")
        }
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        self.git("init", "--template=", "-b", "main")
        self.git("init", "--template=", "--bare", str(self.remote))
        self.git("config", "user.name", "Maintainer")
        self.git("config", "user.email", APPROVED)
        self.git("remote", "add", "origin", str(self.remote))
        # A pre-existing bad identity must not block an ordinary update.
        self.commit("published baseline", author=UNAPPROVED, committer=UNAPPROVED)
        self.git("push", "origin", "main")
        hook = self.repo / ".git" / "hooks" / "pre-push"
        hook.parent.mkdir(exist_ok=True)
        shutil.copyfile(HOOK, hook)
        hook.chmod(0o755)
        self.git("config", "--local", "--add", "ocbox.pushIdentityEmail", APPROVED)

    def git(self, *args, check=True, **env):
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            env=self.env | env,
            capture_output=True,
            text=True,
            timeout=15,
            check=check,
        )

    def commit(self, title, *, author=APPROVED, committer=APPROVED):
        self.git(
            "commit",
            "--allow-empty",
            "-m",
            title,
            GIT_AUTHOR_EMAIL=author,
            GIT_COMMITTER_EMAIL=committer,
        )

    def test_correct_identities_allow_published_branch_and_new_tag(self):
        self.commit("approved")
        self.git("push", "origin", "main")
        self.git("tag", "-am", "release", "v1")
        self.git("push", "origin", "v1")

    def test_bad_author_in_ancestry_blocks_push_even_with_a_good_tip(self):
        self.commit("bad author", author=UNAPPROVED)
        bad = self.git("rev-parse", "--short=12", "HEAD").stdout.strip()
        self.commit("approved tip")
        push = self.git("push", "origin", "main", check=False)
        self.assertNotEqual(push.returncode, 0)
        self.assertIn(f"{bad} has an unapproved author email", push.stderr)

    def test_bad_committer_in_annotated_tag_blocks_push(self):
        self.commit("bad committer", committer=UNAPPROVED)
        self.git("tag", "-am", "release", "v1")
        push = self.git("push", "origin", "v1", check=False)
        self.assertNotEqual(push.returncode, 0)
        self.assertIn("refs/tags/v1", push.stderr)
        self.assertIn("unapproved committer email", push.stderr)

    def test_already_tracked_branch_cannot_bypass_existing_branch_check(self):
        self.commit("unapproved on a feature branch", author=UNAPPROVED)
        # Simulate another clone publishing the same commit first.
        self.git(
            "--git-dir",
            str(self.remote),
            "fetch",
            str(self.repo),
            "HEAD:refs/heads/feature",
        )
        self.git("fetch", "origin")
        push = self.git("push", "origin", "main", check=False)
        self.assertNotEqual(push.returncode, 0)
        self.assertIn("unapproved author email", push.stderr)

    def test_missing_approved_email_blocks_push(self):
        self.git("config", "--local", "--unset-all", "ocbox.pushIdentityEmail")
        self.commit("approved")
        push = self.git("push", "origin", "main", check=False)
        self.assertNotEqual(push.returncode, 0)
        self.assertIn("no allowed email", push.stderr)
