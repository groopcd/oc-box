"""Offline execution of the public POSIX bootstrap and its inline discovery."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
UPSTREAM = "https://github.com/groopcd/oc-box.git"
RELEASES = "https://api.github.com/repos/groopcd/oc-box/releases"
HOSTILE = {
    "PERSONAL_GITHUB_TOKEN": "synthetic-personal",
    "GITHUB_TOKEN": "synthetic-github",
    "GH_TOKEN": "synthetic-gh",
    "CUSTOM_SECRET": "synthetic-secret",
    "OPENCODE_AUTH_CONTENT": "synthetic-auth",
    "SSH_AUTH_SOCK": "/synthetic-agent",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "url.https://bad.invalid/.insteadOf",
    "GIT_CONFIG_VALUE_0": "https://github.com/",
    "GIT_SSH_COMMAND": "synthetic-command",
    "HTTP_PROXY": "http://bad.invalid",
    "HTTPS_PROXY": "http://bad.invalid",
    "ALL_PROXY": "http://bad.invalid",
    "http_proxy": "http://bad.invalid",
    "https_proxy": "http://bad.invalid",
    "PIP_INDEX_URL": "https://bad.invalid/simple",
    "PIP_CONFIG_FILE": "/synthetic-pip",
    "PYTHONPATH": "/synthetic-python",
    "PYTHONHOME": "/synthetic-home",
    "SSL_CERT_FILE": "/synthetic-cert",
}


def release(tag, *, draft=False, prerelease=False):
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease}


class ChannelBootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-channel-bootstrap-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.config = self.root / "actual config"
        self.config.mkdir()
        self.binary = self.root / "bin"
        self.binary.mkdir()
        self.remote = self.root / "remote"
        self.remote.mkdir()
        self.real_git = shutil.which("git")
        self.git_log = self.root / "git.jsonl"
        self.api_log = self.root / "api.jsonl"
        self.python_log = self.root / "python.jsonl"
        self.installed = self.root / "installer.json"
        self.api_spec = self.root / "api.json"
        self.discovery = {"latest": release("v1.6.51"), "pages": [[]]}
        self._fixture_repository()
        self._fixture_commands()

    def git(self, *args):
        return subprocess.run(
            [self.real_git, "-c", "core.hooksPath=/dev/null", *args],
            cwd=self.remote,
            env={
                "PATH": os.environ["PATH"],
                "HOME": str(self.home),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_AUTHOR_NAME": "Fixture",
                "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                "GIT_COMMITTER_NAME": "Fixture",
                "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            },
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()

    def _fixture_repository(self):
        self.git("init", "--quiet", "--initial-branch=master")
        # A tiny offline installer exposes exactly which revision/flags/env ran.
        (self.remote / "install.py").write_text(
            textwrap.dedent(
                f"""\
                import json, os, pathlib, subprocess, sys
                # --with-tui in a comment does not mean this parser supports it.
                source = pathlib.Path(__file__).parent
                def git(*args):
                    return subprocess.check_output(
                        [{self.real_git!r}, "-C", str(source), *args],
                        text=True,
                    ).strip()
                pathlib.Path({str(self.installed)!r}).write_text(json.dumps({{
                    "argv": sys.argv[1:],
                    "env": dict(os.environ),
                    "commit": git("rev-parse", "HEAD"),
                    "head": git("rev-parse", "--abbrev-ref", "HEAD"),
                    "count": git("rev-list", "--count", "HEAD"),
                    "shallow": git("rev-parse", "--is-shallow-repository"),
                    "identity": (source / "identity").read_text(),
                }}))
                """
            )
        )
        (self.remote / "identity").write_text("stable-published")
        self.git("add", "install.py", "identity")
        self.git("commit", "--quiet", "-m", "Published stable fixture")
        self.stable_commit = self.git("rev-parse", "HEAD")
        self.git("tag", "-a", "v1.6.51", "-m", "Annotated stable fixture")
        (self.remote / "identity").write_text("unpublished-master")
        self.git("commit", "--quiet", "-am", "Moving master fixture")
        self.git("checkout", "--quiet", "-b", "devel")
        (self.remote / "identity").write_text("devel-published")
        implementation = self.remote / "src/oc_box/install.py"
        implementation.parent.mkdir(parents=True)
        implementation.write_text(
            'SUPPORTED_OPTION = "--with-tui"\n'
            'raise RuntimeError("The capability probe must not execute installer code")\n'
        )
        self.git("add", "src/oc_box/install.py")
        self.git("commit", "--quiet", "-am", "Published development fixture")
        self.devel_commit = self.git("rev-parse", "HEAD")
        for tag in ("v1.7.52.dev0", "v1.7.9.dev0", "v1.7.10.dev0"):
            self.git("tag", tag)
        (self.remote / "identity").write_text("unpublished-devel")
        self.git("commit", "--quiet", "-am", "Moving devel fixture")
        self.git("checkout", "--quiet", "-b", "foreign", "master")
        (self.remote / "identity").write_text("foreign")
        self.git("commit", "--quiet", "-am", "Foreign release fixture")
        self.git("tag", "v9.0.0")
        self.git("tag", "v9.0.0.dev0")
        self.git("checkout", "--quiet", "master")

    def executable(self, name, content):
        target = self.binary / name
        target.write_text(content)
        target.chmod(0o755)

    def _fixture_commands(self):
        self.executable("id", '#!/bin/sh\nprintf "1000\\n"\n')
        # All Git operations are real; only the fixed public URL is transported
        # to a private fixture repository. Record before allowing local transport.
        self.executable(
            "git",
            f"#!{sys.executable} -I\n"
            + textwrap.dedent(
                f"""\
                import json, os, pathlib, subprocess, sys
                args = sys.argv[1:]
                with pathlib.Path({str(self.git_log)!r}).open("a") as log:
                    log.write(json.dumps({{"argv": args, "env": dict(os.environ)}}) + "\\n")
                if "clone" in args or "fetch" in args:
                    assert args[-2] == {UPSTREAM!r}, "Unexpected Git network endpoint"
                else:
                    assert any(command in args for command in (
                        "rev-parse", "merge-base", "checkout",
                    )), "Unexpected Git command"
                spec = json.loads(pathlib.Path({str(self.api_spec)!r}).read_text())
                if "rev-parse" in args and "git_commit" in spec:
                    sys.stdout.write(spec["git_commit"])
                    sys.exit(0)
                args = [{str(self.remote)!r} if arg == {UPSTREAM!r} else arg for arg in args]
                result = subprocess.run(
                    [{self.real_git!r}, "-c", "protocol.file.allow=always", *args],
                    check=False,
                )
                sys.exit(result.returncode)
                """
            ),
        )
        # Execute the actual inline Python, mocking only urllib's transport.
        # No fixture selector needs to survive the bootstrap's env -i boundary.
        self.executable(
            "python3",
            f"#!{sys.executable} -I\n"
            + textwrap.dedent(
                f"""\
                import io, json, os, pathlib, socket, sys
                from urllib import error, request
                with pathlib.Path({str(self.python_log)!r}).open("a") as log:
                    log.write(json.dumps({{
                        "argv": sys.argv[1:], "env": dict(os.environ),
                    }}) + "\\n")
                if sys.argv[1:3] != ["-I", "-"] or sys.argv[3] not in ("stable", "devel"):
                    os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
                code = sys.stdin.read()
                spec = json.loads(pathlib.Path({str(self.api_spec)!r}).read_text())
                def blocked(*args, **kwargs):
                    raise AssertionError("A bootstrap test attempted a real network call")
                socket.socket = blocked
                def build_opener(*handlers):
                    assert isinstance(handlers[0], request.ProxyHandler)
                    assert handlers[0].proxies == {{}}
                    class Opener:
                        def open(self, req, timeout):
                            with pathlib.Path({str(self.api_log)!r}).open("a") as log:
                                log.write(json.dumps({{
                                    "url": req.full_url,
                                    "headers": dict(req.header_items()),
                                    "timeout": timeout,
                                    "env": dict(os.environ),
                                }}) + "\\n")
                            if spec.get("error"):
                                raise error.URLError("synthetic-private-error")
                            if spec.get("redirect"):
                                handlers[1].redirect_request(
                                    req, None, 302, "redirect", {{}},
                                    "https://bad.invalid/releases",
                                )
                                raise AssertionError("Redirect was not refused")
                            if "raw" in spec:
                                return io.BytesIO(spec["raw"].encode())
                            if spec.get("oversized"):
                                return io.BytesIO(b"x" * (4 * 1024 * 1024 + 1))
                            if req.full_url == {RELEASES + "/latest"!r}:
                                body = spec["latest"]
                            else:
                                prefix = {RELEASES + "?per_page=100&page="!r}
                                assert req.full_url.startswith(prefix), req.full_url
                                page = int(req.full_url[len(prefix):])
                                body = spec["pages"][page - 1]
                            return io.BytesIO(json.dumps(body).encode())
                    return Opener()
                request.build_opener = build_opener
                sys.argv = ["-", *sys.argv[3:]]
                exec(compile(code, "<bootstrap-discovery>", "exec"))
                """
            ),
        )

    def run_bootstrap(self, *args, xdg=True):
        self.api_spec.write_text(json.dumps(self.discovery))
        env = {
            **os.environ,
            **HOSTILE,
            "HOME": str(self.home),
            "PATH": str(self.binary) + ":" + os.environ["PATH"],
            "TMPDIR": str(self.root),
        }
        if xdg:
            env["XDG_CONFIG_HOME"] = str(self.config)
        else:
            env.pop("XDG_CONFIG_HOME", None)
        result = subprocess.run(
            ["sh", str(APP / "install.sh"), *args],
            check=False,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(list(self.root.glob("oc-box-install.*")), [])
        return result

    def records(self, path):
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )

    def assert_sanitized(self, env):
        for key in HOSTILE:
            self.assertNotIn(key, env, key)
        self.assertNotIn("LD_PRELOAD", env)

    def assert_refused(self, result, message=None):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.installed.exists())
        self.assertFalse((self.home / ".local/bin/oc-box").exists())
        self.assertNotIn("synthetic-private-error", result.stderr)
        if message:
            self.assertIn(message, result.stderr)

    def test_posix_syntax(self):
        result = subprocess.run(
            ["sh", "-n", str(APP / "install.sh")],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_root_refuses_before_discovery_or_git(self):
        self.executable("id", '#!/bin/sh\nprintf "0\\n"\n')
        self.assert_refused(self.run_bootstrap(), "regular host user")
        self.assertFalse(self.api_log.exists())
        self.assertFalse(self.git_log.exists())

    def test_default_pins_published_stable_not_moving_master_and_preserves_config(self):
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = json.loads(self.installed.read_text())
        self.assertEqual(installed["argv"], ["--stable", "--release-version", "1.6.51"])
        self.assertEqual(installed["commit"], self.stable_commit)
        self.assertEqual(installed["identity"], "stable-published")
        self.assertEqual(installed["head"], "HEAD")
        self.assertEqual(installed["shallow"], "false")
        self.assertEqual(installed["env"]["XDG_CONFIG_HOME"], str(self.config))
        self.assertEqual(installed["env"]["HOME"], str(self.home))
        self.assert_sanitized(installed["env"])
        for call in self.records(self.python_log):
            self.assert_sanitized(call["env"])
        api = self.records(self.api_log)
        self.assertEqual([call["url"] for call in api], [RELEASES + "/latest"])
        for call in api:
            self.assertEqual(call["timeout"], 15)
            self.assertNotIn("Authorization", call["headers"])
            self.assert_sanitized(call["env"])
        commands = self.records(self.git_log)
        for call in commands:
            self.assert_sanitized(call["env"])
            self.assertEqual(call["env"]["GIT_CONFIG_GLOBAL"], "/dev/null")
            self.assertEqual(call["env"]["GIT_CONFIG_NOSYSTEM"], "1")
            self.assertNotEqual(call["env"]["HOME"], str(self.home))
            for setting in (
                "credential.helper=",
                "core.hooksPath=/dev/null",
                "http.followRedirects=false",
                "protocol.allow=never",
                "protocol.https.allow=always",
            ):
                self.assertIn(setting, call["argv"])
            self.assertNotIn("--depth", call["argv"])
            self.assertNotIn("--shallow-since", call["argv"])
        clone = next(call["argv"] for call in commands if "clone" in call["argv"])
        self.assertEqual(clone[clone.index("--branch") + 1], "master")
        self.assertIn(UPSTREAM, clone)
        fetch = next(call["argv"] for call in commands if "fetch" in call["argv"])
        self.assertIn(UPSTREAM, fetch)
        self.assertIn("refs/tags/v1.6.51:refs/tags/v1.6.51", fetch)
        self.assertTrue(any("merge-base" in call["argv"] for call in commands))

    def test_explicit_stable_with_vault_and_default_config_home(self):
        result = self.run_bootstrap("--with-vault", "--stable", xdg=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = json.loads(self.installed.read_text())
        self.assertEqual(
            installed["argv"],
            ["--stable", "--release-version", "1.6.51", "--with-vault"],
        )
        self.assertEqual(
            installed["env"]["XDG_CONFIG_HOME"], str(self.home / ".config")
        )

    def test_devel_uses_numeric_maximum_across_complete_pages_and_supports_extras(self):
        ignored = release("v99.0.0.dev0", draft=True, prerelease=True)
        self.discovery["pages"] = [
            [release("v1.7.9.dev0", prerelease=True)] + [ignored] * 99,
            [
                release("v1.7.10.dev0", prerelease=True),
                release("v9.0.0.dev0"),  # Flag mismatch.
                release("v9.0.0", prerelease=True),  # Final tag mismatch.
                release("v99.0.0.dev0+gabc", prerelease=True),
                release("v99.0.0.dev1", prerelease=True),
                release("v01.7.99.dev0", prerelease=True),
                release("v99.0.0.dev0", draft=0, prerelease=True),
                release("v99.0.0.dev0", prerelease=1),
            ],
        ]
        result = self.run_bootstrap("--with-vault", "--devel", "--with-tui")
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = json.loads(self.installed.read_text())
        self.assertEqual(
            installed["argv"],
            [
                "--devel",
                "--release-version",
                "1.7.10.dev0",
                "--with-vault",
                "--with-tui",
            ],
        )
        self.assertEqual(installed["identity"], "devel-published")
        self.assertEqual(installed["commit"], self.devel_commit)
        self.assertEqual(installed["count"], "3")
        self.assertEqual(installed["head"], "HEAD")
        self.assertEqual(installed["shallow"], "false")
        for call in self.records(self.python_log):
            self.assert_sanitized(call["env"])
        clone = next(
            call["argv"]
            for call in self.records(self.git_log)
            if "clone" in call["argv"]
        )
        self.assertEqual(clone[clone.index("--branch") + 1], "devel")
        self.assertEqual(
            [call["url"] for call in self.records(self.api_log)],
            [RELEASES + f"?per_page=100&page={page}" for page in (1, 2)],
        )

    def test_invalid_and_contradictory_options_refuse_before_discovery(self):
        for args in (
            ("--stable", "--devel"),
            ("--devel", "--stable"),
            ("--stable", "--with-tui", "--devel"),
            ("--branch", "foreign"),
            ("--release-version", "9.0.0"),
        ):
            with self.subTest(args=args):
                result = self.run_bootstrap(*args)
                self.assert_refused(result)
                self.assertFalse(self.api_log.exists())
                self.assertFalse(self.git_log.exists())

    def test_legacy_stable_without_tui_refuses_after_verified_checkout(self):
        for args in (
            ("--with-tui",),
            ("--stable", "--with-tui", "--with-vault"),
        ):
            with self.subTest(args=args):
                result = self.run_bootstrap(*args)
                self.assert_refused(result, "does not support --with-tui")
                self.assertTrue(self.api_log.exists())
                commands = self.records(self.git_log)
                self.assertTrue(any("merge-base" in call["argv"] for call in commands))
                self.assertTrue(any("checkout" in call["argv"] for call in commands))
                for call in self.records(self.python_log):
                    self.assert_sanitized(call["env"])

    def test_promoted_stable_accepts_tui_when_package_installer_supports_it(self):
        self.git("merge", "--quiet", "--ff-only", self.devel_commit)
        self.git("tag", "v1.7.52")
        self.discovery["latest"] = release("v1.7.52")
        result = self.run_bootstrap("--stable", "--with-tui")
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = json.loads(self.installed.read_text())
        self.assertEqual(
            installed["argv"],
            ["--stable", "--release-version", "1.7.52", "--with-tui"],
        )
        self.assertEqual(installed["commit"], self.devel_commit)

    def test_tui_capability_falls_back_to_root_installer(self):
        with (self.remote / "install.py").open("a") as installer:
            installer.write('SUPPORTED_OPTION = "--with-tui"\n')
        self.git("commit", "--quiet", "-am", "Root installer supports TUI")
        self.git("tag", "v1.7.53")
        self.discovery["latest"] = release("v1.7.53")
        result = self.run_bootstrap("--with-tui")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(self.installed.read_text())["argv"],
            ["--stable", "--release-version", "1.7.53", "--with-tui"],
        )

    def test_devel_tui_checks_package_installer_not_root_or_channel(self):
        self.git("checkout", "--quiet", "devel")
        with (self.remote / "install.py").open("a") as installer:
            installer.write('SUPPORTED_OPTION = "--with-tui"\n')
        (self.remote / "src/oc_box/install.py").write_text(
            "# A comment mentioning --with-tui is not an option declaration.\n"
            'DOCUMENTATION = "Mentioning --with-tui inside prose is not enough."\n'
            'raise RuntimeError("Do not execute the installer to inspect it")\n'
        )
        self.git("commit", "--quiet", "-am", "Development installer without TUI")
        self.git("tag", "v1.7.53.dev0")
        self.discovery["pages"] = [[release("v1.7.53.dev0", prerelease=True)]]
        self.assert_refused(
            self.run_bootstrap("--devel", "--with-tui"), "does not support --with-tui"
        )
        self.assertTrue(
            any("checkout" in call["argv"] for call in self.records(self.git_log))
        )

    def test_malformed_installer_capability_probe_refuses_without_execution(self):
        self.git("checkout", "--quiet", "devel")
        (self.remote / "src/oc_box/install.py").write_text(
            'SUPPORTED_OPTION = "--with-tui"\nthis is not valid Python!\n'
        )
        self.git("commit", "--quiet", "-am", "Malformed development installer")
        self.git("tag", "v1.7.53.dev0")
        self.discovery["pages"] = [[release("v1.7.53.dev0", prerelease=True)]]
        self.assert_refused(
            self.run_bootstrap("--devel", "--with-tui"), "Cannot inspect"
        )

    def test_invalid_resolved_commit_ids_refuse_before_lineage_or_checkout(self):
        for commit in (
            "",
            "a" * 39,
            "a" * 41,
            "A" * 40,
            "--help",
            "a" * 40 + "\n" + "b" * 40,
            "a" * 39 + " ",
        ):
            with self.subTest(commit=commit):
                self.discovery["git_commit"] = commit
                self.assert_refused(self.run_bootstrap(), "commit ID is invalid")
                for call in self.records(self.git_log):
                    self.assertNotIn("merge-base", call["argv"])
                    self.assertNotIn("checkout", call["argv"])

    def test_invalid_stable_release_never_clones(self):
        for record in (
            {},
            [],
            release("v1.7.52.dev0"),
            release("v1.6.51", prerelease=True),
            release("v1.6.51", draft=True),
            release("v1.6.51", draft=0),
            release("v1.6.51", prerelease=0),
            release("v1.6.51+gabc"),
            release("v01.6.51"),
            release("v1.6.51;bad"),
            release("../master"),
            release(123),
        ):
            with self.subTest(record=record):
                self.discovery["latest"] = record
                self.assert_refused(self.run_bootstrap(), "published stable")
                self.assertFalse(self.git_log.exists())

    def test_no_devel_or_invalid_inventory_refuses_without_cloning(self):
        for pages in (
            [[]],
            [[release("v1.6.51")]],
            [{}],
            [[None]],
            [[release("v1.7.52.dev0", prerelease=True)] * 101],
        ):
            with self.subTest(pages=pages):
                self.discovery["pages"] = pages
                self.assert_refused(self.run_bootstrap("--devel"))
                self.assertFalse(self.git_log.exists())

    def test_full_tenth_page_refuses_incomplete_inventory_even_with_candidate(self):
        self.discovery["pages"] = [
            [release("v1.7.52.dev0", prerelease=True)] * 100
        ] * 10
        self.assert_refused(self.run_bootstrap("--devel"), "pagination limit")
        self.assertEqual(len(self.records(self.api_log)), 10)
        self.assertFalse(self.git_log.exists())

    def test_short_tenth_page_completes_inventory(self):
        self.discovery["pages"] = [
            [release("v99.0.0.dev0", draft=True, prerelease=True)] * 100
        ] * 9 + [[release("v1.7.52.dev0", prerelease=True)]]
        result = self.run_bootstrap("--devel")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.records(self.api_log)), 10)
        self.assertEqual(
            json.loads(self.installed.read_text())["commit"], self.devel_commit
        )

    def test_discovery_transport_redirect_size_json_and_network_fail_closed(self):
        for spec, message in (
            ({"redirect": True}, "redirect"),
            ({"oversized": True}, "response limit"),
            ({"raw": "not-json"}, "Public release discovery failed"),
            ({"error": True}, "Public release discovery failed"),
        ):
            with self.subTest(spec=spec):
                self.discovery = spec
                self.assert_refused(self.run_bootstrap(), message)
                self.assertFalse(self.git_log.exists())

    def test_missing_published_tag_never_runs_installer(self):
        self.discovery["latest"] = release("v8.0.0")
        self.assert_refused(self.run_bootstrap())
        self.assertFalse(
            any("checkout" in call["argv"] for call in self.records(self.git_log))
        )

    def test_published_tags_outside_channel_lineage_never_run_installer(self):
        for channel, tag in (("stable", "v9.0.0"), ("devel", "v9.0.0.dev0")):
            with self.subTest(channel=channel):
                self.discovery = {
                    "latest": release(tag),
                    "pages": [[release(tag, prerelease=True)]],
                }
                self.assert_refused(
                    self.run_bootstrap("--" + channel), "not on the required"
                )
                self.assertFalse(
                    any(
                        "checkout" in call["argv"]
                        for call in self.records(self.git_log)
                    )
                )


if __name__ == "__main__":
    unittest.main()
