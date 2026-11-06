import contextlib
import importlib.util
import io
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
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
    def test_every_parser_command_and_option_has_its_own_reference_entry(self):
        text = (APP / "docs/cli.md").read_text()
        headings = re.findall(r"^### (.+)$", text, re.MULTILINE)
        options = {
            value
            for heading in headings
            for value in re.findall(r"(?<![\w-])--?[a-z][a-z-]*", heading)
        }
        parser = box.parser()
        expected = {
            value for action in parser._actions for value in action.option_strings
        }
        self.assertEqual(options, expected)
        commands = next(
            action.choices for action in parser._actions if action.dest == "command"
        )
        self.assertTrue(set(commands).issubset(headings))
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
        ):
            self.assertIn(phrase, text)

    def test_public_examples_do_not_publish_development_session_ids_or_home_paths(self):
        for name in (
            "README.md",
            "AGENTS.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "docs/cli.md",
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
            self.assertEqual(build.main(["--output-dir", temporary]), 0)
            self.assertEqual(
                (Path(temporary) / "oc-box.info").read_bytes(),
                (APP / "docs/oc-box.info").read_bytes(),
            )

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
