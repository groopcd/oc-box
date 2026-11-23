"""Bounded offline evaluation of the actual announcement routing/argv wiring.

This is not a GitHub Actions runner. Evaluate the small boolean expressions used
by these jobs and execute their actual preflight/argv scripts with synthetic
inputs; no GitHub or Buffer credentials, services or mutations are involved.
"""

import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


APP = Path(__file__).resolve().parents[1]


def job(source, name):
    return re.search(
        r"^  " + re.escape(name) + r":\n(.*?)(?=^  [A-Za-z_]+:|\Z)",
        source,
        re.MULTILINE | re.DOTALL,
    )[1]


def expression(source, name):
    return re.search(
        r"^\s+" + re.escape(name) + r": \$\{\{ (.+) \}\}$",
        source,
        re.MULTILINE,
    )[1]


def condition(source):
    return " ".join(
        re.search(r"^    if: >-\n((?:      .+\n)+)", source, re.MULTILINE)[1].split()
    )


def inline_python(source, step_name=None):
    if step_name is not None:
        source = source.split("      - name: " + step_name + "\n", 1)[1]
    return textwrap.dedent(
        source.split("python -I - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
    )


def evaluate(value, context):
    value = value.replace("&&", " and ").replace("||", " or ")
    value = re.sub(r"!(?=[A-Za-z_(])", " not ", value).strip()
    for key in sorted(context, key=len, reverse=True):
        value = value.replace(key, repr(context[key]))
    return eval(
        value,
        {
            "__builtins__": {},
            "endsWith": lambda s, suffix: s.endswith(suffix),
            "contains": lambda s, part: part.casefold() in s.casefold(),
        },
    )


class AnnouncementWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.publisher = (APP / ".github/workflows/publish.yml").read_text()
        cls.linkedin = (APP / ".github/workflows/linkedin.yml").read_text()
        cls.hook = job(cls.publisher, "linkedin")

    def context(self, **changes):
        return {
            "github.event.release.tag_name": "",
            "github.event.release.body": "",
            "inputs.release_tag": "",
            "inputs.skip_linkedin": False,
            "vars.LINKEDIN_ANNOUNCEMENTS": "true",
            "needs.publish.result": "success",
            "needs.build.outputs.publish_validated": "true",
            "secrets.BUFFER_API_KEY": "synthetic-only",
            **changes,
        }

    def test_release_event_and_manual_publication_devel_only_preview_without_key(self):
        for source in ("github.event.release.tag_name", "inputs.release_tag"):
            for enabled in ("true", "false", ""):
                context = self.context(
                    **{
                        source: "v1.7.250.dev0",
                        "vars.LINKEDIN_ANNOUNCEMENTS": enabled,
                    }
                )
                with self.subTest(source=source, enabled=enabled):
                    self.assertTrue(evaluate(condition(self.hook), context))
                    self.assertEqual(
                        evaluate(expression(self.hook, "action"), context), "preview"
                    )
                    self.assertEqual(
                        evaluate(expression(self.hook, "BUFFER_API_KEY"), context), ""
                    )
                    self.assertEqual(
                        evaluate(expression(self.hook, "release_tag"), context),
                        "v1.7.250.dev0",
                    )

    def test_per_publication_skip_matrix_preserves_default_channel_routing(self):
        routes = (
            ("release-default", "github.event.release.tag_name", None, "", True),
            (
                "release-body",
                "github.event.release.tag_name",
                "## Highlights",
                "",
                True,
            ),
            (
                "release-skip",
                "github.event.release.tag_name",
                "## Highlights\n\n<!-- linkedin:skip -->\n",
                "",
                False,
            ),
            (
                "release-legacy-stable-skip",
                "github.event.release.tag_name",
                "## Highlights\n\n<!-- oc-box: skip-linkedin -->\n",
                "",
                False,
            ),
            (
                "release-unrelated-marker",
                "github.event.release.tag_name",
                "<!-- linkedin-image:readme-tui -->",
                "",
                True,
            ),
            ("dispatch-default", "inputs.release_tag", None, False, True),
            ("dispatch-skip", "inputs.release_tag", None, True, False),
        )
        for tag in ("v1.6.99", "v1.7.250.dev0"):
            for enabled in ("true", "false", ""):
                for route, source, body, skip, allowed in routes:
                    context = self.context(
                        **{
                            source: tag,
                            "github.event.release.body": body,
                            "inputs.skip_linkedin": skip,
                            "vars.LINKEDIN_ANNOUNCEMENTS": enabled,
                        }
                    )
                    with self.subTest(tag=tag, enabled=enabled, route=route):
                        self.assertEqual(
                            bool(evaluate(condition(self.hook), context)),
                            allowed and (tag.endswith(".dev0") or enabled == "true"),
                        )

    def test_stable_auto_posting_retains_enable_and_publish_success_gates(self):
        for source in ("github.event.release.tag_name", "inputs.release_tag"):
            for enabled in ("true", "false", ""):
                context = self.context(
                    **{
                        source: "v1.6.99",
                        "vars.LINKEDIN_ANNOUNCEMENTS": enabled,
                    }
                )
                with self.subTest(source=source, enabled=enabled):
                    self.assertEqual(
                        bool(evaluate(condition(self.hook), context)), enabled == "true"
                    )
                    self.assertEqual(
                        evaluate(expression(self.hook, "action"), context), "announce"
                    )
                    self.assertEqual(
                        evaluate(expression(self.hook, "BUFFER_API_KEY"), context),
                        "synthetic-only",
                    )
        for tag in ("v1.6.99", "v1.7.250.dev0"):
            for state in ("failure", "skipped", "cancelled"):
                context = self.context(
                    **{
                        "inputs.release_tag": tag,
                        "needs.publish.result": state,
                    }
                )
                self.assertFalse(evaluate(condition(self.hook), context))
            for validated in ("", "false"):
                context = self.context(
                    **{
                        "inputs.release_tag": tag,
                        "needs.build.outputs.publish_validated": validated,
                    }
                )
                self.assertFalse(evaluate(condition(self.hook), context))

    def test_reusable_default_is_explicit_and_blank_or_preview_never_selects_posting(
        self,
    ):
        call = self.linkedin.split("  workflow_call:\n", 1)[1].split(
            "  workflow_dispatch:", 1
        )[0]
        self.assertRegex(call, r"action:\n(?:[^\n]*\n)*?        default: announce")
        for mode in ("", "preview", "channels", "announce", "reconcile", "invalid"):
            for enabled in ("true", "false"):
                context = {
                    "github.repository": "groopcd/oc-box",
                    "inputs.action": mode,
                    "vars.LINKEDIN_ANNOUNCEMENTS": enabled,
                }
                with self.subTest(mode=mode, enabled=enabled):
                    self.assertEqual(
                        bool(
                            evaluate(
                                condition(job(self.linkedin, "announcement")), context
                            )
                        ),
                        mode in ("announce", "reconcile") and enabled == "true",
                    )
                    self.assertEqual(
                        bool(
                            evaluate(condition(job(self.linkedin, "inspect")), context)
                        ),
                        mode in ("preview", "channels"),
                    )
        self.assertNotIn("inputs.action ||", self.linkedin)

    def test_actual_preflight_scripts_reject_bad_action_or_tag(self):
        for name, modes in (
            ("inspect", ("channels", "preview")),
            ("announcement", ("announce", "reconcile")),
        ):
            source = job(self.linkedin, name)
            if name == "announcement":
                script = inline_python(source)
            else:
                script = textwrap.dedent(
                    source.split("python - <<'PY'\n", 1)[1].split("\n          PY", 1)[
                        0
                    ]
                )
            for mode in (*modes, "", "invalid"):
                for tag in ("v1.7.250.dev0", "", "v1.7.250.dev0;false", "main"):
                    environment = {
                        "ACTION": mode,
                        "RELEASE_TAG": tag,
                        "POST_ID": "post_123" if mode == "reconcile" else "",
                        "EXPECTED_TEXT_SHA256": "a" * 64 if mode == "announce" else "",
                        "EXPECTED_IMAGE_SHA256": "b" * 64 if mode == "announce" else "",
                        "PATH": os.defpath,
                    }
                    result = subprocess.run(
                        ["python3", "-c", script],
                        env=environment,
                        capture_output=True,
                        timeout=10,
                    )
                    good = mode in modes and (
                        tag == "" if mode == "channels" else tag == "v1.7.250.dev0"
                    )
                    with self.subTest(job=name, mode=mode, tag=tag):
                        self.assertEqual(result.returncode == 0, good)

    def test_manual_approval_hashes_are_quoted_argv_data_not_shell_interpolation(self):
        source = job(self.linkedin, "announcement")
        script = textwrap.dedent(source.rsplit("        run: |\n", 1)[1])
        for name in ("EXPECTED_TEXT_SHA256", "EXPECTED_IMAGE_SHA256"):
            self.assertIn(name + ": ${{ inputs.", source)
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            interpreter = root / "linkedin-tools/bin/python"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_text("#!/bin/sh\nprintf '%s\\0' \"$@\"\n")
            interpreter.chmod(0o700)
            hashes = ("a" * 64, '$(touch SHOULD_NOT_EXIST)"; false; #')
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=root,
                env={
                    "PATH": os.defpath,
                    "RUNNER_TEMP": work,
                    "ACTION": "announce",
                    "RELEASE_TAG": "v1.7.250.dev0",
                    "POST_ID": "",
                    "EXPECTED_TEXT_SHA256": hashes[0],
                    "EXPECTED_IMAGE_SHA256": hashes[1],
                },
                capture_output=True,
                timeout=10,
                check=True,
            )
            self.assertEqual(
                result.stdout.decode().split("\0")[:-1],
                [
                    "-B",
                    "tools/announce_release.py",
                    "announce",
                    "--tag",
                    "v1.7.250.dev0",
                    "--expected-text-sha256",
                    hashes[0],
                    "--expected-image-sha256",
                    hashes[1],
                ],
            )
            self.assertFalse((root / "SHOULD_NOT_EXIST").exists())


class SelectedDevelToolFenceTests(unittest.TestCase):
    """Execute the trusted workflow's checks, never the selected tag's code."""

    @classmethod
    def setUpClass(cls):
        source = (APP / ".github/workflows/linkedin.yml").read_text()
        cls.announcement = job(source, "announcement")
        cls.preflight = inline_python(
            cls.announcement, "Validate tag and approval before selecting source"
        )
        cls.fence = inline_python(
            cls.announcement,
            "Verify selected devel tool approval fence without executing it",
        )
        cls.current_tool = (APP / "tools/announce_release.py").read_bytes()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="devel-tool-fence-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = {
            "PATH": os.defpath,
            "HOME": str(self.root),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "ACTION": "announce",
            "RELEASE_TAG": "v1.7.250.dev0",
            "POST_ID": "",
            "EXPECTED_TEXT_SHA256": "a" * 64,
            "EXPECTED_IMAGE_SHA256": "b" * 64,
        }

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            env=self.env,
            check=True,
            capture_output=True,
            timeout=10,
        ).stdout

    def selected_tool(self, data):
        path = self.root / "tools/announce_release.py"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        self.git("init", "--initial-branch=devel")
        self.git("add", "tools/announce_release.py")
        self.git("commit", "-m", "Synthetic selected release")
        self.git("tag", self.env["RELEASE_TAG"])
        return path

    def run_script(self, script, **environment):
        return subprocess.run(
            [sys.executable, "-I", "-c", script],
            cwd=self.root,
            env={**self.env, **environment},
            capture_output=True,
            timeout=10,
        )

    def test_devel_dispatch_hash_matrix_refuses_before_checkout(self):
        for text, image, accepted in (
            ("", "", False),
            ("a" * 64, "", False),
            ("", "b" * 64, False),
            ("a" * 63, "b" * 64, False),
            ("a" * 65, "b" * 64, False),
            ("A" * 64, "b" * 64, False),
            (" a" * 32, "b" * 64, False),
            ("$(touch SHOULD_NOT_EXIST)", "b" * 64, False),
            ("a" * 64, "b" * 64, True),
        ):
            with self.subTest(text=text, image=image):
                result = self.run_script(
                    self.preflight,
                    EXPECTED_TEXT_SHA256=text,
                    EXPECTED_IMAGE_SHA256=image,
                )
                self.assertEqual(result.returncode == 0, accepted)
                self.assertFalse((self.root / ".git").exists())
                self.assertFalse((self.root / "SHOULD_NOT_EXIST").exists())
                if not accepted:
                    self.assertEqual(
                        result.stderr.strip(),
                        b"Release announcement refused: invalid-input-or-approval",
                    )

    def test_old_tag_tool_cannot_reach_secret_stage_with_blank_or_supplied_hashes(self):
        old = (
            b"# Historical release tool: no approval-policy declaration.\n"
            b"from pathlib import Path\n"
            b"Path('SELECTED_TOOL_EXECUTED').write_text('shareNow')\n"
        )
        self.selected_tool(old)
        for text, image in (("", ""), ("a" * 64, "b" * 64)):
            with self.subTest(supplied=bool(text)):
                stages = []
                first = self.run_script(
                    self.preflight,
                    EXPECTED_TEXT_SHA256=text,
                    EXPECTED_IMAGE_SHA256=image,
                )
                if first.returncode == 0:
                    stages.append("checkout")
                    second = self.run_script(self.fence)
                    if second.returncode == 0:
                        stages.append("Buffer-key-step")
                    else:
                        self.assertEqual(
                            second.stderr.strip(),
                            b"Release announcement refused: unverified-devel-approval-tool",
                        )
                self.assertNotIn("Buffer-key-step", stages)
                self.assertEqual(stages, ["checkout"] if text else [])
                self.assertFalse((self.root / "SELECTED_TOOL_EXECUTED").exists())

    def test_stable_automatic_announce_still_accepts_no_approval_inputs(self):
        result = self.run_script(
            self.preflight,
            RELEASE_TAG="v1.6.99",
            EXPECTED_TEXT_SHA256="",
            EXPECTED_IMAGE_SHA256="",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_current_policy_tool_passes_read_only_canonical_byte_check(self):
        self.selected_tool(self.current_tool)
        self.assertEqual(self.run_script(self.preflight).returncode, 0)
        result = self.run_script(self.fence)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("status", "--porcelain"), b"")
        self.assertFalse((self.root / "SELECTED_TOOL_EXECUTED").exists())

    def test_marker_parser_refuses_comment_string_unknown_computed_or_duplicate_policy(
        self,
    ):
        name = b"DEVEL_ANNOUNCEMENT_APPROVAL_POLICY"
        for index, declaration in enumerate(
            (
                b"# " + name + b" = 1\n",
                b'"' + name + b' = 1"\n',
                name + b" = 2\n",
                name + b" = True\n",
                name + b" = 1 + 0\n",
                name + b": int = 1\n",
                name + b" = 1\n" + name + b" = 1\n",
                b"if True:\n    " + name + b" = 1\n",
            )
        ):
            with self.subTest(declaration=declaration):
                # Each selection has its own synthetic tag and actual Git blob.
                self.env["RELEASE_TAG"] = "v1.7." + str(251 + index) + ".dev0"
                self.selected_tool(
                    declaration
                    + b"raise RuntimeError('selected code must never execute')\n"
                )
                result = self.run_script(self.fence)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(b"selected code must never execute", result.stderr)

    def test_working_file_hash_or_head_tag_mismatch_refuses_before_secret_stage(self):
        path = self.selected_tool(self.current_tool)
        path.write_bytes(self.current_tool + b"\n# changed after checkout\n")
        self.assertNotEqual(self.run_script(self.fence).returncode, 0)
        self.git("add", "tools/announce_release.py")
        self.git("commit", "-m", "Synthetic HEAD no longer selected tag")
        self.assertNotEqual(self.run_script(self.fence).returncode, 0)

    def test_reconcile_requires_post_id_and_verified_tool_but_no_new_post_approval(
        self,
    ):
        self.selected_tool(b"# Historical pre-fence tool.\n")
        environment = {
            "ACTION": "reconcile",
            "POST_ID": "post_123",
            "EXPECTED_TEXT_SHA256": "",
            "EXPECTED_IMAGE_SHA256": "",
        }
        self.assertEqual(self.run_script(self.preflight, **environment).returncode, 0)
        self.assertNotEqual(self.run_script(self.fence, **environment).returncode, 0)
        for change in (
            {"POST_ID": ""},
            {"POST_ID": "bad post"},
            {"EXPECTED_TEXT_SHA256": "a" * 64},
        ):
            with self.subTest(change=change):
                self.assertNotEqual(
                    self.run_script(
                        self.preflight, **{**environment, **change}
                    ).returncode,
                    0,
                )

    def test_workflow_orders_checks_before_checkout_execution_and_buffer_key_exposure(
        self,
    ):
        source = self.announcement
        before = source.index("name: Validate tag and approval before selecting source")
        checkout = source.index("uses: actions/checkout@")
        verify = source.index("name: Verify selected devel tool approval fence")
        dependencies = source.index("name: Install isolated card renderer")
        secret = source.index("BUFFER_API_KEY: ${{ secrets.BUFFER_API_KEY }}")
        execution = source.index('args=("$ACTION")')
        self.assertLess(before, checkout)
        self.assertLess(checkout, verify)
        self.assertLess(verify, dependencies)
        self.assertLess(dependencies, secret)
        self.assertLess(secret, execution)
        self.assertIn("ref: refs/tags/${{ inputs.release_tag }}", source)
        self.assertIn("if: endsWith(inputs.release_tag, '.dev0')", source)
        self.assertNotIn("BUFFER_API_KEY", source[:secret])
        self.assertNotIn("tools/announce_release.py", source[:verify])
        self.assertNotIn("exec(", self.fence)
        self.assertNotIn("eval(", self.fence)


if __name__ == "__main__":
    unittest.main()
