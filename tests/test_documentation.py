import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from oc_box import backups, dependencies, oc_box as box, workspace_service
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(
        "oc_box." + name if path.parent.name == "oc_box" else name, path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = load("build_docs", APP / "tools/build_docs.py")


class DocumentationTests(unittest.TestCase):
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

    def test_generated_sources_match_the_reference(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(build.main(["--check"]), 0)

    def test_source_only_check_needs_no_renderer_but_info_content_check_does(self):
        with (
            patch.object(build.shutil, "which", return_value=None),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(build.main(["--check"]), 0)
            with self.assertRaises(SystemExit) as refused:
                build.main(["--check-info-content"])
        self.assertEqual(refused.exception.code, 1)
        self.assertIn("GNU makeinfo", errors.getvalue())

    def test_info_content_check_refuses_unidentified_renderer(self):
        with (
            patch.object(build.shutil, "which", return_value="/synthetic/makeinfo"),
            patch.object(
                build.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 0, stdout="Other renderer\n"
                ),
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as errors,
            self.assertRaises(SystemExit) as refused,
        ):
            build.main(["--check-info-content"])
        self.assertEqual(refused.exception.code, 1)
        self.assertIn("identified GNU makeinfo version", errors.getvalue())

    @unittest.skipUnless(shutil.which("makeinfo"), "GNU makeinfo not installed")
    def test_info_content_check_rejects_changed_body_with_unchanged_headings(self):
        original = "# Synthetic reference\n\n## Records\n\nKeep synthetic records.\n"
        changed = original.replace("Keep synthetic records.", "Drop synthetic records.")
        with (
            tempfile.TemporaryDirectory(prefix="oc-box-info-freshness-") as temporary,
            contextlib.redirect_stdout(io.StringIO()),
            patch.object(build, "ROOT", Path(temporary)),
        ):
            docs = Path(temporary) / "docs"
            docs.mkdir()
            (docs / "cli.md").write_text(original)
            output = ["--output-dir", str(docs)]
            self.assertEqual(build.main(output), 0)
            info = (docs / "oc-box.info").read_bytes()
            self.assertIn(b"Keep synthetic records.", info)
            self.assertNotIn(b"Drop synthetic records.", info)
            self.assertEqual(build.main(["--check-info-content", *output]), 0)
            other_renderer = re.sub(
                rb"makeinfo version [0-9.]+", b"makeinfo version 0.0", info
            )
            (docs / "oc-box.info").write_bytes(other_renderer)
            self.assertEqual(build.main(["--check-info-content", *output]), 0)

            # Only the body changed. Refresh the two sources, deliberately leaving
            # the previously rendered Info untouched: headings alone cannot tell.
            (docs / "cli.md").write_text(changed)
            (docs / "oc-box.1").write_text(build.render_man(changed))
            (docs / "oc-box.texi").write_text(build.render_texinfo(changed))
            self.assertEqual(build.main(["--check", *output]), 0)
            for name, artifact in (
                ("same-renderer", info),
                ("different-renderer", other_renderer),
            ):
                with self.subTest(name=name):
                    (docs / "oc-box.info").write_bytes(artifact)
                    with (
                        contextlib.redirect_stderr(io.StringIO()) as errors,
                        self.assertRaises(SystemExit) as refused,
                    ):
                        build.main(["--check-info-content", *output])
                    self.assertEqual(refused.exception.code, 1)
                    self.assertIn("oc-box.info", errors.getvalue())
                    self.assertEqual((docs / "oc-box.info").read_bytes(), artifact)

    def test_info_content_normalization_preserves_body_and_navigation(self):
        # Independently specified tiny Info documents, not snapshots of prose in
        # the public manual. GNU releases may rewrap and requote the same text.
        first = (
            "This is oc-box.info, produced by makeinfo version 7.1 from oc-box.texi.\n\n"
            "\x1f\nFile: oc-box.info,  Node: Top,  Up: (dir)\n\n"
            "Records\n*******\n\nKeep `synthetic' records.\n\n"
            "\x1f\nTag Table:\nNode: Top\x7f100\n\x1f\nEnd Tag Table\n"
            "\n\x1f\nLocal Variables:\ncoding: utf-8\nEnd:\n"
        )
        second = (
            first.replace("version 7.1", "version 7.3")
            .replace("Keep `synthetic' records.", "Keep ‘synthetic’\nrecords.")
            .replace("Top\x7f100", "Top\x7f104")
        )
        self.assertEqual(build.info_content(first), build.info_content(second))
        for name, changed in (
            ("body", second.replace("Keep", "Drop")),
            ("navigation", second.replace("Up: (dir)", "Up: Elsewhere")),
            ("missing-body", second.replace("Keep ‘synthetic’\nrecords.", "")),
            ("shell-backticks", second.replace("‘synthetic’", "`synthetic`")),
        ):
            with self.subTest(name=name):
                self.assertNotEqual(
                    build.info_content(first), build.info_content(changed)
                )

    @unittest.skipUnless(shutil.which("makeinfo"), "GNU makeinfo not installed")
    def test_info_content_check_preserves_literal_example_bytes(self):
        cases = (
            (
                "argument-spaces",
                "printf '%s\\n' 'two  words'",
                "printf '%s\\n' 'two words'",
            ),
            ("literal-apostrophe", 'printf "%s\\n" "a’b"', 'printf "%s\\n" "a\'b"'),
            ("indentation", "if True:\n  print('x')", "if True:\n print('x')"),
            ("line-break", "printf one\nprintf two", "printf one printf two"),
            ("blank-line", "printf one\n\nprintf two", "printf one\nprintf two"),
            (
                "nonbreaking-space",
                "printf 'two\N{NO-BREAK SPACE}words'",
                "printf 'two words'",
            ),
        )
        with (
            tempfile.TemporaryDirectory(prefix="oc-box-info-literals-") as temporary,
            contextlib.redirect_stdout(io.StringIO()),
            patch.object(build, "ROOT", Path(temporary)),
        ):
            docs = Path(temporary) / "docs"
            docs.mkdir()
            output = ["--output-dir", str(docs)]
            for name, old, new in cases:
                with self.subTest(name=name):
                    original = "# Synthetic\n\n## Records\n\n```sh\n" + old + "\n```\n"
                    changed = "# Synthetic\n\n## Records\n\n```sh\n" + new + "\n```\n"
                    (docs / "cli.md").write_text(original)
                    self.assertEqual(build.main(output), 0)
                    info = (docs / "oc-box.info").read_bytes()
                    for line in old.split("\n"):
                        if line:
                            self.assertIn(b"     " + line.encode("utf-8") + b"\n", info)
                    stale = re.sub(
                        rb"makeinfo version [0-9.]+", b"makeinfo version 0.0", info
                    )
                    (docs / "oc-box.info").write_bytes(stale)
                    self.assertEqual(build.main(["--check-info-content", *output]), 0)
                    (docs / "cli.md").write_text(changed)
                    (docs / "oc-box.1").write_text(build.render_man(changed))
                    (docs / "oc-box.texi").write_text(build.render_texinfo(changed))
                    with (
                        contextlib.redirect_stderr(io.StringIO()) as errors,
                        self.assertRaises(SystemExit) as refused,
                    ):
                        build.main(["--check-info-content", *output])
                    self.assertEqual(refused.exception.code, 1)
                    self.assertIn("oc-box.info", errors.getvalue())
                    self.assertEqual((docs / "oc-box.info").read_bytes(), stale)

    def test_info_content_compares_tag_labels_and_validates_terminal_structure(self):
        first = (
            "This is oc-box.info, produced by makeinfo version 7.1 from oc-box.texi.\n\n"
            "\x1f\nFile: oc-box.info,  Node: Top,  Up: (dir)\n\n"
            "Records\n*******\n\nKeep synthetic records.\n\n"
            "\x1f\nTag Table:\nNode: Top\x7f100\nRef: Records\x7f120\n"
            "\x1f\nEnd Tag Table\n\n\x1f\nLocal Variables:\ncoding: utf-8\nEnd:\n"
        )
        second = (
            "This is oc-box.info, produced by makeinfo version 7.3 from oc-box.texi.\n\n"
            "\x1f\nFile: oc-box.info,  Node: Top,  Up: (dir)\n\n"
            "Records\n*******\n\nKeep synthetic records.\n\n"
            "\x1f\nTag Table:\nNode: Top\x7f104\nRef: Records\x7f124\n"
            "\x1f\nEnd Tag Table\n\n\x1f\nLocal Variables:\ncoding: utf-8\nEnd:\n"
        )
        self.assertEqual(build.info_content(first), build.info_content(second))
        self.assertNotEqual(
            build.info_content(first),
            build.info_content(second.replace("Ref: Records\x7f", "Ref: Changed\x7f")),
        )
        marker = "\x1f\nTag Table:\n"
        for name, invalid in (
            ("missing-table", second.split(marker)[0]),
            ("truncated-at-table", second.split(marker)[0] + marker),
            ("missing-terminator", second.replace("\x1f\nEnd Tag Table\n", "")),
            ("missing-footer", second.split("\n\x1f\nLocal Variables:")[0] + "\n"),
            ("coding", second.replace("coding: utf-8", "coding: latin-1")),
            ("trailing-prose", second + "Unexpected prose: Drop synthetic records.\n"),
            ("duplicate-marker", second.replace(marker, marker + marker)),
            (
                "duplicate-entry",
                second.replace(
                    "Node: Top\x7f104\n", "Node: Top\x7f104\nNode: Top\x7f105\n"
                ),
            ),
            ("nonnumeric-offset", second.replace("Top\x7f104", "Top\x7funknown")),
            ("oversized-offset", second.replace("Top\x7f104", "Top\x7f" + "1" * 13)),
            (
                "oversized-label",
                second.replace("Ref: Records\x7f", "Ref: " + "x" * 257 + "\x7f"),
            ),
            (
                "too-many-tags",
                second.replace(
                    "Ref: Records\x7f124\n",
                    "".join(
                        "Ref: tag" + str(index) + "\x7f124\n" for index in range(1024)
                    ),
                ),
            ),
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                build.info_content(invalid)

    def test_info_prose_wrapping_keeps_quoted_code_and_unicode_content(self):
        prefix = (
            "This is oc-box.info, produced by makeinfo version 7.1 from oc-box.texi.\n\n"
            "\x1f\nFile: oc-box.info,  Node: Top,  Up: (dir)\n\n"
        )
        suffix = (
            "\n\n\x1f\nTag Table:\nNode: Top\x7f100\n\x1f\nEnd Tag Table\n"
            "\n\x1f\nLocal Variables:\ncoding: utf-8\nEnd:\n"
        )
        old = prefix + "Keep `two  words' safely.  Then continue." + suffix
        equivalent = (
            prefix.replace("version 7.1", "version 7.3")
            + "Keep ‘two  words’\nsafely.  Then\ncontinue."
            + suffix
        )
        self.assertEqual(build.info_content(old), build.info_content(equivalent))
        for name, old_body, new_body in (
            ("quoted-spaces", "Use ‘two  words’.", "Use ‘two words’."),
            ("quoted-apostrophe", "Use ‘a’b’.", "Use ‘a'b’."),
            (
                "nonbreaking-space",
                "Keep two\N{NO-BREAK SPACE}words.",
                "Keep two words.",
            ),
        ):
            with self.subTest(name=name):
                self.assertNotEqual(
                    build.info_content(prefix + old_body + suffix),
                    build.info_content(
                        prefix.replace("version 7.1", "version 7.3") + new_body + suffix
                    ),
                )

    @unittest.skipUnless(shutil.which("makeinfo"), "GNU makeinfo not installed")
    def test_info_content_check_rejects_malformed_footer_and_suffix(self):
        with (
            tempfile.TemporaryDirectory(prefix="oc-box-info-footer-") as temporary,
            contextlib.redirect_stdout(io.StringIO()),
            patch.object(build, "ROOT", Path(temporary)),
        ):
            docs = Path(temporary) / "docs"
            docs.mkdir()
            (docs / "cli.md").write_text("# Synthetic\n\n## Records\n\nKeep records.\n")
            output = ["--output-dir", str(docs)]
            self.assertEqual(build.main(output), 0)
            info = re.sub(
                rb"makeinfo version [0-9.]+",
                b"makeinfo version 0.0",
                (docs / "oc-box.info").read_bytes(),
            )
            marker = b"\x1f\nTag Table:\n"
            for name, invalid in (
                ("truncated-at-table", info.split(marker)[0] + marker),
                ("missing-terminator", info.replace(b"\x1f\nEnd Tag Table\n", b"")),
                ("missing-footer", info.split(b"\n\x1f\nLocal Variables:")[0] + b"\n"),
                ("coding", info.replace(b"coding: utf-8", b"coding: latin-1")),
                (
                    "trailing-prose",
                    info + b"Unexpected prose: Drop synthetic records.\n",
                ),
                ("duplicate-marker", info.replace(marker, marker + marker)),
                (
                    "changed-label",
                    info.replace(b"Node: Records\x7f", b"Node: Different\x7f"),
                ),
            ):
                with self.subTest(name=name):
                    (docs / "oc-box.info").write_bytes(invalid)
                    with (
                        contextlib.redirect_stderr(io.StringIO()) as errors,
                        self.assertRaises(SystemExit) as refused,
                    ):
                        build.main(["--check-info-content", *output])
                    self.assertEqual(refused.exception.code, 1)
                    self.assertIn("oc-box.info", errors.getvalue())
                    self.assertEqual((docs / "oc-box.info").read_bytes(), invalid)

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
            # Cross-version checks must compare body content too, not just nodes.
            self.assertEqual(build.main(["--check-info-content"]), 0)

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
                patch.object(shutil, "which", return_value=None),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                from oc_box.install import main as install_main

                self.assertEqual(install_main([]), 0)
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
                contextlib.redirect_stdout(io.StringIO()),
            ):
                from oc_box.install import main as install_main

                self.assertEqual(install_main([]), 0)
                self.assertEqual(install_main([]), 0)
            index = (home / ".local/share/info/dir").read_text()
            self.assertEqual(index.count("* oc-box:"), 1)
            self.assertIn("(oc-box)", index)


if __name__ == "__main__":
    unittest.main()
