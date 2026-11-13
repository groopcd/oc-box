import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import check_release


APP = Path(__file__).resolve().parents[1]


class ReleaseChannelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-release-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "source"
        self.root.mkdir()
        self.env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.git("init", "--initial-branch=master")
        self.source = self.root / check_release.SOURCE
        self.source.parent.mkdir(parents=True)
        self.set_channel("stable")
        # Retain the real absolute baseline, rather than weakening the validator.
        for count in range(28):
            self.git("commit", "--allow-empty", "-m", f"History {count}")
        self.git("update-ref", "refs/remotes/origin/master", "HEAD")

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()

    def set_channel(self, channel, series="1.6"):
        self.source.write_text(
            f'VERSION_SERIES = "{series}"\n'
            f'RELEASE_CHANNEL = "{channel}"\n'
            "VERSION_BASE_COMMIT_COUNT = 29\n"
        )
        self.git("add", check_release.SOURCE)
        self.git("commit", "-m", f"Track {channel} source")

    def release(self, *, channel="stable"):
        build = check_release.validate(self.root)
        self.git("tag", build["tag"])
        return build["tag"], {
            "tag_name": build["tag"],
            "draft": False,
            "prerelease": channel == "devel",
            "published_at": "2026-11-13T00:00:00Z",
        }

    def test_stable_release_uses_exact_tag_commit_and_master_lineage(self):
        tag, release = self.release()
        result = check_release.validate(
            self.root, publish=True, tag=tag, release=release
        )
        self.assertEqual(result["distribution_version"], "1.6.0")
        self.assertEqual(result["release_channel"], "stable")
        self.assertTrue(result["publish_validated"])
        self.assertEqual(result["commit_count"], 29)

    def test_devel_release_uses_dev0_and_devel_lineage(self):
        self.git("checkout", "-b", "devel")
        self.set_channel("devel", series="1.7")
        self.git("update-ref", "refs/remotes/origin/devel", "HEAD")
        tag, release = self.release(channel="devel")
        result = check_release.validate(
            self.root, publish=True, tag=tag, release=release
        )
        self.assertEqual(tag, "v1.7.1.dev0")
        self.assertEqual(result["release_channel"], "devel")
        self.assertTrue(result["publish_validated"])
        with self.assertRaisesRegex(ValueError, "prerelease"):
            check_release.validate(
                self.root,
                publish=True,
                tag=tag,
                release={**release, "prerelease": False},
            )

    def test_build_only_allows_feature_branch_without_release_or_lineage(self):
        self.git("checkout", "-b", "feature/package-work")
        self.set_channel("devel")
        result = check_release.validate(self.root)
        self.assertEqual(result["distribution_version"], "1.6.1.dev0")
        self.assertFalse(result["publish_validated"])
        self.assertEqual(result["commit"], self.git("rev-parse", "HEAD"))

    def test_publishing_existing_release_is_not_implied_by_a_publish_flag(self):
        for release in (None, {}, [], {"draft": False}):
            with self.subTest(release=release):
                with self.assertRaises(ValueError):
                    check_release.validate(
                        self.root,
                        publish=True,
                        tag="v1.6.0",
                        release=release,
                    )

    def test_release_payload_refusal_matrix(self):
        tag, release = self.release()
        changes = (
            {"tag_name": "v1.6.1"},
            {"tag_name": None},
            {"draft": True},
            {"draft": 0},
            {"draft": None},
            {"prerelease": True},
            {"prerelease": 0},
            {"prerelease": None},
            {"published_at": None},
            {"published_at": ""},
            {"published_at": "not-a-date"},
            {"published_at": "2026-11-13T00:00:00"},
        )
        for change in changes:
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    check_release.validate(
                        self.root,
                        publish=True,
                        tag=tag,
                        release={**release, **change},
                    )
        for wrong in (None, "", "1.6.0", "v1.6.0.dev0", "v1.6.1", "v1.6.0+local"):
            with self.subTest(tag=wrong):
                with self.assertRaisesRegex(ValueError, "Release tag must be"):
                    check_release.validate(
                        self.root, publish=True, tag=wrong, release=release
                    )

    def test_build_only_cannot_accept_unvalidated_release_inputs(self):
        tag, release = self.release()
        for inputs in ({"tag": tag}, {"release": release}):
            with self.subTest(inputs=inputs):
                with self.assertRaisesRegex(ValueError, "explicit publication"):
                    check_release.validate(self.root, **inputs)

    def test_tag_pointing_to_another_commit_is_refused(self):
        tag, release = self.release()
        self.git("tag", "--force", tag, "HEAD^")
        with self.assertRaisesRegex(ValueError, "source commit"):
            check_release.validate(self.root, publish=True, tag=tag, release=release)

    def test_published_payload_does_not_replace_an_actual_git_tag(self):
        release = {
            "tag_name": "v1.6.0",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-11-13T00:00:00Z",
        }
        with self.assertRaises(subprocess.CalledProcessError):
            check_release.validate(
                self.root, publish=True, tag="v1.6.0", release=release
            )

    def test_annotated_tag_is_peeled_to_source_commit(self):
        build = check_release.validate(self.root)
        self.git("tag", "-a", build["tag"], "-m", "Fixture release")
        release = {
            "tag_name": build["tag"],
            "draft": False,
            "prerelease": False,
            "published_at": "2026-11-13T00:00:00Z",
        }
        result = check_release.validate(
            self.root, publish=True, tag=build["tag"], release=release
        )
        self.assertTrue(result["publish_validated"])

    def test_stable_source_outside_master_lineage_is_not_publishable(self):
        self.git("checkout", "-b", "feature/not-released")
        self.git("commit", "--allow-empty", "-m", "Unmerged work")
        tag, release = self.release()
        with self.assertRaisesRegex(ValueError, "master lineage"):
            check_release.validate(self.root, publish=True, tag=tag, release=release)

    def test_devel_cannot_use_master_as_a_substitute_lineage(self):
        self.set_channel("devel")
        self.git("update-ref", "refs/remotes/origin/master", "HEAD")
        tag, release = self.release(channel="devel")
        with self.assertRaisesRegex(ValueError, "devel lineage"):
            check_release.validate(self.root, publish=True, tag=tag, release=release)

    def test_dirty_and_untracked_source_is_refused(self):
        self.source.write_text(self.source.read_text() + "# dirty\n")
        with self.assertRaisesRegex(ValueError, "clean"):
            check_release.validate(self.root)
        self.git("add", check_release.SOURCE)
        with self.assertRaisesRegex(ValueError, "clean"):
            check_release.validate(self.root)
        self.git("commit", "-m", "Fixture dirty change")
        (self.root / "unexpected.txt").write_text("untracked")
        with self.assertRaisesRegex(ValueError, "clean"):
            check_release.validate(self.root)

    def test_shallow_history_is_refused(self):
        shallow = self.root.parent / "shallow"
        self.git("clone", "--depth=1", self.root.as_uri(), str(shallow))
        with self.assertRaisesRegex(ValueError, "Full Git history"):
            check_release.validate(shallow)

    def test_tracked_identity_is_not_an_environment_override(self):
        with patch.dict(
            os.environ, {"RELEASE_CHANNEL": "devel", "OC_BOX_RELEASE_CHANNEL": "devel"}
        ):
            result = check_release.validate(self.root)
        self.assertEqual(result["release_channel"], "stable")
        self.assertEqual(result["distribution_version"], "1.6.0")

    def test_committed_identity_is_read_without_executing_source(self):
        self.source.write_text(
            self.source.read_text() + '\nraise RuntimeError("Do not execute source")\n'
        )
        self.git("add", check_release.SOURCE)
        self.git("commit", "-m", "Untrusted-source fixture")
        self.assertEqual(
            check_release.validate(self.root)["distribution_version"], "1.6.1"
        )

    def test_cli_build_and_manual_publication_routes_use_same_validator(self):
        tag, release = self.release()
        release_file = self.root.parent / "release.json"
        release_file.write_text(json.dumps(release))
        tool = APP / "tools/check_release.py"
        base = [sys.executable, "-B", str(tool), "--root", str(self.root)]
        for extra, published in (
            ([], False),
            (["--publish", "--tag", tag, "--release-json", str(release_file)], True),
        ):
            with self.subTest(extra=extra):
                result = subprocess.run(
                    [*base, *extra],
                    env=self.env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads(result.stdout)["publish_validated"], published
                )
        refused = subprocess.run(
            [*base, "--publish", "--tag", tag],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(refused.returncode, 1)
        self.assertIn("existing published GitHub release", refused.stderr)
        self.assertNotIn("Traceback", refused.stderr)

    def test_workflows_gate_publication_and_enable_devel_checks(self):
        workflow = (APP / ".github/workflows/publish.yml").read_text()
        self.assertIn("release_tag:", workflow)
        self.assertIn("github.event.release.tag_name || inputs.release_tag", workflow)
        self.assertIn("gh api --hostname github.com", workflow)
        self.assertIn("repos/groopcd/oc-box/releases/tags/$RELEASE_TAG", workflow)
        self.assertIn("tools/check_release.py --publish", workflow)
        self.assertIn("needs.build.outputs.publish_validated == 'true'", workflow)
        self.assertIn("name: pypi", workflow)
        self.assertNotIn("skip-existing", workflow)
        for name in ("ci.yml", "distros.yml"):
            with self.subTest(workflow=name):
                self.assertIn(
                    "branches: [master, devel, 'feature/**']",
                    (APP / ".github/workflows" / name).read_text(),
                )


class BuildBackendChannelTests(unittest.TestCase):
    def test_gitless_backend_preserves_channel_and_rejects_relabelled_metadata(self):
        # Exercise the actual backend and runtime version reader, stubbing only
        # the external setuptools boundary so the unit suite stays stdlib-only.
        original_source = (APP / check_release.SOURCE).read_text()
        series, tracked_channel, _ = check_release.source_identity(original_source)
        cases = (
            ("stable", "stable", {}, True),
            ("devel", "devel", {}, True),
            ("stable", "devel", {}, False),
            ("devel", "stable", {}, False),
            ("stable", "stable", {"commit_count": 53}, False),
            ("devel", "devel", {"version": f"{series}.23.dev1+gabcdef0"}, False),
            ("devel", "devel", {"release_channel": None}, False),
        )
        runner = """import json,runpy,sys,types
from pathlib import Path
setuptools=types.ModuleType('setuptools')
def setup(**kwargs):
    target=Path('rebuilt-source')
    kwargs['cmdclass']['sdist']().make_release_tree(target, [])
    print(json.dumps({'version':kwargs['version'], 'build':json.loads((target/'build-info.json').read_text())}))
setuptools.setup=setup
sys.modules['setuptools']=setuptools
for module, name in (('setuptools.command.build_py','build_py'), ('setuptools.command.sdist','sdist')):
    stub=types.ModuleType(module)
    class Command:
        def make_release_tree(self, base_dir, files):
            Path(base_dir).mkdir()
    setattr(stub, name, Command)
    sys.modules[module]=stub
runpy.run_path('src/_oc_box_build.py', run_name='__main__')
"""
        for source_channel, metadata_channel, changes, accepted in cases:
            with (
                self.subTest(
                    source_channel=source_channel,
                    metadata_channel=metadata_channel,
                    changes=changes,
                ),
                tempfile.TemporaryDirectory(
                    prefix="oc-box-gitless-build-"
                ) as temporary,
            ):
                root = Path(temporary)
                package = root / "src/oc_box"
                package.mkdir(parents=True)
                for name in ("__init__.py", "paths.py", "installation.py"):
                    shutil.copyfile(APP / "src/oc_box" / name, package / name)
                # Channel changes are tracked constants, never env switches.
                source = original_source.replace(
                    f'RELEASE_CHANNEL = "{tracked_channel}"',
                    f'RELEASE_CHANNEL = "{source_channel}"',
                )
                (package / "version_info.py").write_text(source)
                shutil.copyfile(
                    APP / "src/_oc_box_build.py", root / "src/_oc_box_build.py"
                )
                public = f"{series}.23" + (
                    ".dev0" if metadata_channel == "devel" else ""
                )
                build = {
                    "version": public + "+gabcdef0",
                    "commit": "abcdef0" * 5 + "abcde",
                    "commit_count": 52,
                    "dirty": False,
                    "release_channel": metadata_channel,
                    **changes,
                }
                (root / "build-info.json").write_text(json.dumps(build))
                result = subprocess.run(
                    [sys.executable, "-B", "-c", runner],
                    cwd=root,
                    env={
                        **os.environ,
                        "PYTHONPATH": "",
                        "RELEASE_CHANNEL": "devel"
                        if source_channel == "stable"
                        else "stable",
                        "OC_BOX_RELEASE_CHANNEL": "devel"
                        if source_channel == "stable"
                        else "stable",
                    },
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if accepted:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        json.loads(result.stdout), {"version": public, "build": build}
                    )
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(
                        "Build metadata does not match the tracked source",
                        result.stderr,
                    )


if __name__ == "__main__":
    unittest.main()
