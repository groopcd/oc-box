import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
import backups
import dependencies
import workspace_service
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


box = load("oc_box_docs", APP / "oc_box.py")
build = load("build_docs", APP / "tools/build_docs.py")
version = load("version_info_docs", APP / "version_info.py")


class DocumentationTests(unittest.TestCase):
    def test_global_vault_host_procedure_is_isolated_and_not_a_live_pass(self):
        for name in ("docs/cli.md", "docs/opencode-2.md", "CONTRIBUTING.md"):
            text = (APP / name).read_text()
            normalized = " ".join(text.split())
            with self.subTest(name=name):
                self.assertIn("tools/test_global_vault_podman.py", text)
                self.assertIn('--opencode-v2 "$(command -v opencode)"', text)
                self.assertIn("--work-dir /var/tmp --temp-dir /var/tmp", text)
                self.assertIn("graphroot/runroot", normalized)
                self.assertIn("scripted", normalized)
                self.assertIn("host", normalized)
        guide = " ".join((APP / "docs/opencode-2.md").read_text().split())
        self.assertIn("No live result for this fixture has been accepted yet", guide)
        self.assertIn("does not install/upgrade your launcher", guide)
        self.assertIn("normal provider accounts", guide)
        self.assertIn("never runs global prune", guide)

    def test_v2_vault_examples_use_native_provider_and_mcp_env_references(self):
        for name in ("docs/cli.md", "docs/opencode-2.md"):
            with self.subTest(name=name):
                text = (APP / name).read_text()
                examples = [
                    json.loads(source)
                    for source in re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)
                ]
                config = next(
                    example
                    for example in examples
                    if "providers" in example and "mcp" in example
                )
                self.assertEqual(config["$schema"], "https://opencode.ai/config.json")
                self.assertEqual(
                    config["providers"]["anthropic"]["settings"]["apiKey"],
                    "{env:ANTHROPIC_API_KEY}",
                )
                self.assertEqual(
                    config["mcp"]["servers"]["example-tools"]["environment"],
                    {"MCP_API_KEY": "{env:MCP_API_KEY}"},
                )

    def test_vault_reference_documents_dynamic_mode_and_invocation_opt_out(self):
        text = (APP / "docs/cli.md").read_text()
        self.assertIn("### --credential-mode env|selected|vault\n", text)
        self.assertIn("--credential-mode vault --clear-credentials", text)
        section = text.split("### --no-vault\n", 1)[1].split("\n### ", 1)[0]
        for phrase in (
            "V2-only",
            "owning",
            "without changing the saved workspace policy",
            "retain their env constraints",
            "Attached TUI/web clients reject",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, section)
        guide = (APP / "docs/opencode-2.md").read_text()
        setup = guide.split(
            "### Global vault-first from the first project launch\n", 1
        )[1]
        setup = setup.split("\n### ", 1)[0]
        self.assertLess(
            setup.index("oc-box secrets set ANTHROPIC_API_KEY"),
            setup.index("oc-box --project ~/devel/demo"),
        )
        self.assertNotIn("oc-box credentials", setup)
        self.assertNotRegex(text + guide, r"(?m)^oc-box\s+--(?:auto|standalone)\b")

    def test_native_autoapproval_is_not_the_container_build_only_default(self):
        text = " ".join((APP / "docs/opencode-2.md").read_text().split())
        for phrase in (
            "inside container Build mode only",
            "All non-Build/Plan/child rules",
            "opencode --auto --standalone",
            "does not override explicit denials",
            "session.permissions",
            "oc-box does not enable it or modify normal host `cli.json`",
            "No new live permission-prompt/reload pass has been accepted yet",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text.replace("**", ""))

    def test_global_vault_keeps_native_oauth_copying_independent(self):
        for name in ("docs/cli.md", "docs/opencode-2.md"):
            text = " ".join((APP / name).read_text().split())
            with self.subTest(name=name):
                self.assertIn(
                    "Native OpenCode saved/OAuth credential refresh and copying "
                    "remain independent in every credential mode, including global vault mode",
                    text,
                )
                self.assertIn("structured OAuth refresh-token records", text)
                self.assertIn("`OPENCODE_AUTH_CONTENT`", text)
                self.assertIn("no plaintext opencode json secrets", text.lower())
        for name in (
            "README.md",
            "SECURITY.md",
            "CONTRIBUTING.md",
            "AGENTS.md",
            "docs/cli.md",
            "docs/opencode-2.md",
        ):
            with self.subTest(name=name):
                text = " ".join((APP / name).read_text().split())
                self.assertNotIn("env/auth fallback", text)
                self.assertNotIn("env-file/OpenCode-auth", text)

    def test_v2_build_helper_preserves_denials_and_child_rules(self):
        for name in ("docs/cli.md", "docs/opencode-2.md"):
            text = " ".join((APP / name).read_text().split()).replace("**", "")
            with self.subTest(name=name):
                self.assertRegex(text, r"final `ask`(?: decision)? to `allow`")
                self.assertIn("explicit permission event agent wins", text)
                self.assertRegex(text, r"never(?: assumes)? the parent's agent")
                self.assertIn("denials remain denied", text)
                self.assertIn("Explicit session and mandatory policy denials", text)
                self.assertIn("never overwrites a deny passed to it", text)
                self.assertIn("including their configured denies, as before", text)
                self.assertIn("existing final Build", text)
                self.assertIn("Session permission arrays are not overwritten", text)
                self.assertIn("Plan/child requests", text)
                self.assertIn("elicitation prompts", text)
                self.assertIn("`OPENCODE_CONFIG_CONTENT` overlay", text)
                self.assertIn("managed policy still wins", text)
                self.assertIn("No `--auto`", text)
                self.assertIn("native evaluator/import/reload", text)

    def test_every_parser_command_and_option_has_its_own_reference_entry(self):
        text = (APP / "docs/cli.md").read_text()
        headings = re.findall(r"^### (.+)$", text, re.MULTILINE)
        options = {
            value
            for heading in headings
            for value in re.findall(r"(?<![\w-])--?[a-z][a-z-]*", heading)
        }
        parser = box.parser()
        parsers = [
            parser,
            dependencies.parser(),
            workspace_service.web_parser(),
            box.secrets_parser(),
            box.completion_parser(),
            *(
                backups.parser_for(action)
                for action in ("backup", "backup-info", "restore")
            ),
        ]
        expected = {
            value
            for current in parsers
            for action in current._actions
            for value in action.option_strings
        }
        self.assertEqual(options, expected)
        commands = next(
            action.choices for action in parser._actions if action.dest == "command"
        )
        self.assertTrue(
            set(commands).issubset({heading.split(" — ", 1)[0] for heading in headings})
        )
        for name, (image, _) in box.DISTROS.items():
            self.assertIn(image, text, name)
        for alias in box.ALIASES:
            self.assertIn("`" + alias + "`", text)
        for action in parser._actions:
            if action.option_strings:
                heading = next(
                    heading
                    for heading in headings
                    if action.option_strings[0] in heading
                )
                section = (
                    text.split("### " + heading + "\n", 1)[1]
                    .split("\n### ", 1)[0]
                    .split("\n## ", 1)[0]
                )
                self.assertGreater(len(section.strip()), 80, heading)

    def test_generated_sources_match_the_reference(self):
        text = (APP / "docs/cli.md").read_text()
        self.assertEqual((APP / "docs/oc-box.1").read_text(), build.render_man(text))
        self.assertEqual(
            (APP / "docs/oc-box.texi").read_text(), build.render_texinfo(text)
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(build.main(["--check"]), 0)

    def test_info_includes_navigation_examples_and_lifecycle_details(self):
        text = (APP / "docs/oc-box.info").read_text()
        for phrase in (
            "Node: Top",
            "Node: Commands",
            "Node: Options",
            "Node: Lifecycle and persistence",
            "oc-box reset --project",
            "--import-session",
            "--clear-podman-args",
            "container_use_devices",
            "not live updates",
            "GPL-3.0-or-later",
            "marker missing",
            "marker mismatch",
            "retired/ID/",
            "name_to_handle_at",
            "project_filesystem_identity",
            "capability-based",
            "filesystem-independent",
            "auto_update",
            "oc-box upgrade --check",
            "curl -fsSL",
            "oc-box backup",
            "restore_incomplete",
            "--recipient",
            "container_project",
        ):
            self.assertIn(phrase, text)

    def test_public_examples_do_not_publish_development_session_ids_or_home_paths(self):
        for name in (
            "README.md",
            "AGENTS.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "docs/cli.md",
            "docs/opencode-2.md",
        ):
            with self.subTest(name=name):
                text = (APP / name).read_text()
                self.assertNotRegex(text, r"ses_[0-9a-f]{12,}")
                self.assertNotRegex(text, r"/home/[a-zA-Z0-9_.-]+/")

    def test_converters_escape_terminal_source_syntax(self):
        text = "# Test\n\n## Name\n\nUse `--flag` with user@example.org and {braces}.\n\n```sh\n.hidden\\file\n'quoted\n```\n"
        man = build.render_man(text)
        self.assertIn(r"\fB\-\-flag\fR", man)
        self.assertIn(r"\&.hidden\efile", man)
        self.assertIn(r"\&'quoted", man)
        tex = build.render_texinfo(text)
        self.assertIn("user@@example.org", tex)
        self.assertIn("@{braces@}", tex)
        self.assertIn("@code{--flag}", tex)

    @unittest.skipUnless(shutil.which("groff"), "groff not installed")
    def test_man_renders_without_roff_warnings(self):
        result = subprocess.run(
            ["groff", "-Tutf8", "-man", "-z", str(APP / "docs/oc-box.1")],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    @unittest.skipUnless(shutil.which("makeinfo"), "GNU makeinfo not installed")
    def test_info_rebuild_is_reproducible(self):
        with (
            tempfile.TemporaryDirectory(prefix="oc-box-doc-rebuild-") as temporary,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            first = Path(temporary) / "first"
            second = Path(temporary) / "second"
            self.assertEqual(build.main(["--output-dir", str(first)]), 0)
            self.assertEqual(build.main(["--output-dir", str(second)]), 0)
            self.assertEqual(
                (first / "oc-box.info").read_bytes(),
                (second / "oc-box.info").read_bytes(),
            )
            # makeinfo embeds its version and can change wrapping, typography,
            # and tag-table offsets between releases (e.g. 7.1 vs 7.3). Exact
            # byte reproducibility applies within one toolchain, not across
            # arbitrary host versions. Sources are checked separately above.
            generated = (first / "oc-box.info").read_text()
            committed = (APP / "docs/oc-box.info").read_text()
            nodes = r"^File: [^\n]*?Node: ([^,\n]+)"
            self.assertEqual(
                re.findall(nodes, generated, re.MULTILINE),
                re.findall(nodes, committed, re.MULTILINE),
            )
            self.assertIn("--import-session", generated)
            self.assertIn("oc-box upgrade --check", generated)
            if generated.splitlines()[0] == committed.splitlines()[0]:
                self.assertEqual(generated, committed)

    @unittest.skipUnless(shutil.which("info"), "GNU Info reader not installed")
    def test_info_reader_can_load_the_options_node(self):
        with tempfile.TemporaryDirectory(prefix="oc-box-info-read-") as temporary:
            output = Path(temporary) / "rendered.txt"
            result = subprocess.run(
                [
                    "info",
                    "--file",
                    str(APP / "docs/oc-box.info"),
                    "--node=Options",
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--import-session", output.read_text())
            self.assertIn("--podman-arg", output.read_text())

    def test_installer_copies_prebuilt_manuals_without_documentation_build_tools(self):
        with tempfile.TemporaryDirectory(prefix="oc-box-doc-install-") as temporary:
            home = Path(temporary)
            with (
                patch.object(Path, "home", return_value=home),
                patch.dict(sys.modules, {"version_info": version}),
                patch.object(shutil, "which", return_value=None),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                runpy.run_path(str(APP / "install.py"))
            expected = {
                home / ".local/share/man/man1/oc-box.1": APP / "docs/oc-box.1",
                home / ".local/share/info/oc-box.info": APP / "docs/oc-box.info",
            }
            for destination, source in expected.items():
                self.assertEqual(destination.read_bytes(), source.read_bytes())
            for name in ("cli.md", "oc-box.1", "oc-box.texi", "oc-box.info"):
                self.assertEqual(
                    (home / ".local/share/oc-box/docs" / name).read_bytes(),
                    (APP / "docs" / name).read_bytes(),
                )
            self.assertFalse((home / ".local/share/info/dir").exists())
            self.assertTrue(os.access(home / ".local/bin/oc-box", os.X_OK))
            for name in ("SECURITY.md", "CONTRIBUTING.md"):
                self.assertEqual(
                    (home / ".local/share/oc-box" / name).read_bytes(),
                    (APP / name).read_bytes(),
                )

    @unittest.skipUnless(
        shutil.which("install-info") or Path("/usr/sbin/install-info").is_file(),
        "install-info unavailable",
    )
    def test_installer_updates_only_the_private_info_directory(self):
        with tempfile.TemporaryDirectory(prefix="oc-box-info-install-") as temporary:
            home = Path(temporary)
            with (
                patch.object(Path, "home", return_value=home),
                patch.dict(sys.modules, {"version_info": version}),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                runpy.run_path(str(APP / "install.py"))
                runpy.run_path(str(APP / "install.py"))
            index = (home / ".local/share/info/dir").read_text()
            self.assertEqual(index.count("* oc-box:"), 1)
            self.assertIn("(oc-box)", index)


if __name__ == "__main__":
    unittest.main()
