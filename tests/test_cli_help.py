"""Scoped launcher help must be useful without touching any host state."""

import contextlib
import io
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oc_box import activity_log
from oc_box import oc_box as box

_COMMANDS = (
    "attach",
    "backup",
    "backup-info",
    "completion",
    "credentials",
    "distros",
    "doctor",
    "list",
    "logs",
    "preferences",
    "purge",
    "repair",
    "reset",
    "restore",
    "secrets",
    "share",
    "start",
    "status",
    "stop",
    "sync",
    "terminal",
    "tui",
    "upgrade",
    "web",
)
_SELECTORS = "--project --workspace"
_LAUNCH_LOGGING = "--log-dir --journald --no-journald --lifecycle-diagnostics"

_GUARDED_ENTRYPOINT = """\
import sys

# Launcher journaling deliberately swallows Exception, so use a separate guard.
class UnexpectedHelpAccess(BaseException):
    pass

def deny_external_access(event, args):
    if event in ("subprocess.Popen", "os.exec", "os.posix_spawn", "os.system",
                 "socket.connect", "socket.sendto", "sqlite3.connect"):
        raise UnexpectedHelpAccess(event)

sys.addaudithook(deny_external_access)
sys.path.insert(0, sys.argv.pop(1))
from oc_box import activity_log, oc_box

def refuse(boundary):
    def blocked(*args, **kwargs):
        raise UnexpectedHelpAccess(boundary)
    return blocked

oc_box.check_backend = refuse("Podman preflight")
oc_box.state_root = refuse("workspace state")
oc_box.find_workspace = refuse("workspace lookup")
oc_box.workspace_lock = refuse("workspace lock")
oc_box.vault.vault_path = refuse("vault")
activity_log.record_event = refuse("activity journal append")
activity_log.read_events = refuse("activity journal read")
oc_box.log_config._read_config = refuse("host log configuration")
sys.exit(oc_box.entrypoint())
"""


class CliHelpTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="oc-box-help-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.project = self.root / "project with spaces"
        self.project.mkdir()
        self.env = {
            "HOME": str(self.root / "home"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "XDG_STATE_HOME": str(self.root / "xdg-state"),
            "OC_BOX_STATE_HOME": str(self.root / "registry"),
            "OC_BOX_CONTAINER": "0",
            "PATH": str(self.root / "no-executables"),
            "LC_ALL": "C",
            "TERM": "dumb",
            "COLUMNS": "96",
        }

    def help(self, *args, command=None):
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-c",
                _GUARDED_ENTRYPOINT,
                str(box.PACKAGE.parent),
                *args,
            ],
            check=False,
            cwd=self.project,
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(list(self.root.iterdir()), [self.project])
        self.assertEqual(list(self.project.iterdir()), [])
        prog = "oc-box" if command is None else f"oc-box {command}"
        self.assertRegex(result.stdout, rf"\Ausage:\s+{re.escape(prog)}(?:\s|$)")
        return result.stdout

    def test_equals_short_help_cluster_never_journals(self):
        argv = ["purge", "-y=h"]
        # Older argparse interprets this as '-y -h'; newer versions can
        # reject it. Preserve the parser result and avoid either journal write.
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as parsed,
        ):
            box.parser().parse_args(argv)
        with (
            patch.object(sys, "argv", ["oc-box", *argv]),
            patch.object(activity_log, "record_event") as journal,
            patch.object(box, "check_backend", side_effect=AssertionError("Podman")),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as launched,
        ):
            box.entrypoint()
        self.assertEqual(launched.exception.code, parsed.exception.code)
        journal.assert_not_called()

    def test_dash_prefixed_project_value_does_not_disable_lifecycle_reporting(self):
        def launch():
            self.assertTrue(box.lifecycle_output.concise())
            return 0

        with (
            patch.object(
                sys, "argv", ["oc-box", "start", "--project", "-draft checkout"]
            ),
            patch.object(box, "main", side_effect=launch),
            patch.object(activity_log, "record_event", return_value=True) as journal,
        ):
            self.assertEqual(box.entrypoint(), 0)
        self.assertEqual(
            [call.args[0] for call in journal.call_args_list],
            ["command.started", "command.finished"],
        )

    def assert_options(self, output, *, show="", hide=""):
        # Match complete flag names in both usage and the body: --new is not
        # --no-new, --vault is not --no-vault, and --all is not --allow-*.
        options = set(re.findall(r"(?<![\w-])--?[a-z][a-z-]*(?![\w-])", output))
        self.assertEqual(set(show.split()) - options, set(), output)
        self.assertEqual(set(hide.split()) & options, set(), output)

    @staticmethod
    def help_sections(output):
        return re.findall(r"(?m)^[^:\n]+:\n((?:[ \t]+[^\n]+\n)+)", output)

    def test_root_is_a_short_command_index_not_a_flag_catalog(self):
        output = self.help("--help")
        self.assertLessEqual(len(output.splitlines()), 60)
        self.assertRegex(output, r"(?i)oc-box\s+command\s+--help")
        known_commands = set(_COMMANDS)
        groups = [
            known_commands.intersection(
                re.findall(r"(?m)^[ \t]+([a-z][a-z-]*)[ \t]+\S", section)
            )
            for section in self.help_sections(output)
        ]
        self.assertGreaterEqual(sum(bool(group) for group in groups), 2, output)
        self.assertEqual(set().union(*groups), known_commands, output)
        self.assert_options(
            output,
            show="--help --status --version --about",
            hide=(
                "--new --resume --import-session --cpus --device --no-vault "
                "--all --delete-sessions --yes --maintenance --log-dir --journald"
            ),
        )

    def test_each_explicit_command_prints_its_own_scoped_help(self):
        selector_logging = f"{_SELECTORS} {_LAUNCH_LOGGING}"
        scope = {
            "start": (
                (
                    f"{selector_logging} --new --resume --import-session --model "
                    "--distro --cpus --memory --pids-limit --device --podman-arg "
                    "--host-drop --no-vault"
                ),
                "--all --delete-sessions --maintenance --credential-mode --check --status --version",
            ),
            "reset": (
                (
                    f"{selector_logging} --new --resume --model --distro --cpus "
                    "--memory --pids-limit --device --podman-arg --clear-devices "
                    "--clear-podman-args --no-vault"
                ),
                "--import-session --all --delete-sessions --maintenance",
            ),
            "sync": (
                f"{selector_logging} --cpus --memory --pids-limit",
                "--new --import-session --device --no-vault --maintenance --all --status",
            ),
            "stop": (
                selector_logging,
                "--resume --model --device --no-vault --maintenance --delete-sessions",
            ),
            "status": (
                f"{_SELECTORS} --json",
                f"{_LAUNCH_LOGGING} --new --cpus --all --check",
            ),
            "list": ("", f"{_SELECTORS} --json --distro --new --journald --all"),
            "distros": ("", f"{_SELECTORS} --json --distro --new --journald --all"),
            "upgrade": (
                "--check --stable --devel --vault --tui --auto-update",
                f"{_SELECTORS} --new --no-vault --journald --all",
            ),
            "credentials": (
                (
                    f"{_SELECTORS} --credential-mode --env-secret --vault-secret "
                    "--clear-credentials"
                ),
                "--vault --no-vault --new --journald --all",
            ),
            "purge": (
                f"{_SELECTORS} --all --delete-sessions -y --yes",
                "--new --cpus --device --log-dir --journald --credential-mode",
            ),
            "repair": (_SELECTORS, "--new --all --delete-sessions --journald"),
            "terminal": (
                f"{selector_logging} --maintenance",
                "--new --import-session --device --no-vault --delete-sessions",
            ),
        }
        for command in _COMMANDS:
            with self.subTest(command=command):
                output = self.help(command, "--help", command=command)
                show, hide = scope.get(command, ("", ""))
                self.assert_options(output, show=f"--help {show}", hide=hide)

    def test_start_help_groups_workspace_session_and_logging_options(self):
        output = self.help("start", "--help", command="start")
        groups = [
            set(re.findall(r"(?m)^[ \t]+(--[a-z][a-z-]*)\b", section))
            for section in self.help_sections(output)
        ]
        matches = [
            [index for index, flags in enumerate(groups) if flag in flags]
            for flag in ("--project", "--new", "--journald")
        ]
        self.assertEqual([len(indices) for indices in matches], [1, 1, 1], output)
        self.assertEqual(len({indices[0] for indices in matches}), 3, output)

    def test_dedicated_logs_and_web_help_distinguish_reading_from_mirroring(self):
        logs = self.help("logs", "--help", command="logs")
        self.assert_options(
            logs,
            show=f"{_SELECTORS} --journald --log-dir --path --level --limit --follow --json",
            hide="--no-journald --lifecycle-diagnostics --port --no-open",
        )
        self.assertRegex(logs, r"(?im)^[ \t]+--journald\b\s+[^\n]*\bread\b")

        web = self.help("web", "--help", command="web")
        self.assert_options(
            web,
            show=f"{_SELECTORS} {_LAUNCH_LOGGING} --port --no-open --session --no-vault",
            hide="--path --level --limit --follow --json --new",
        )
        self.assertRegex(web, r"(?im)^[ \t]+--journald\b\s+[^\n]*\bmirror\b")

    def test_selector_first_help_uses_the_command_not_a_selector_value(self):
        cases = (
            (("--project", "sync", "start", "--help"), "start", "--import-session"),
            (("--project", str(self.project), "sync", "--help"), "sync", "--log-dir"),
            (("--workspace=reset", "stop", "-h"), "stop", "--no-journald"),
        )
        for args, command, option in cases:
            with self.subTest(args=args):
                output = self.help(*args, command=command)
                self.assert_options(
                    output,
                    show=f"{_SELECTORS} {option}",
                    hide="--all" if command == "start" else "--import-session",
                )

    def test_alternate_help_spellings_never_enter_journaled_commands(self):
        for command, option, requested in (
            ("purge", "--all", "-yh"),
            ("repair", "--project", "--hel"),
        ):
            with self.subTest(command=command, requested=requested):
                output = self.help(command, requested, command=command)
                self.assert_options(output, show=f"--help {option}", hide="--new")

    def test_status_alias_help_uses_status_scope_not_default_start(self):
        output = self.help("--status", "--help")
        self.assert_options(
            output,
            show=f"{_SELECTORS} --json",
            hide=f"{_LAUNCH_LOGGING} --new --import-session --cpus --all",
        )


class LauncherParserCompatibilityTests(unittest.TestCase):
    def test_implicit_start_preserves_abbreviations_and_types(self):
        parser = box.parser()
        self.assertEqual(parser.parse_args([]).command, "start")
        options = ["--proj", "sync", "--new", "--pids-limit", "128", "--no-journald"]
        implicit = parser.parse_args(options)
        for explicit in (["start", *options], [*options, "start"]):
            with self.subTest(explicit=explicit):
                self.assertEqual(vars(parser.parse_args(explicit)), vars(implicit))
        self.assertEqual(implicit.project, Path("sync"))
        self.assertTrue(implicit.new)
        self.assertEqual(implicit.pids_limit, 128)
        self.assertIs(implicit.journald, False)

    def test_help_filter_does_not_change_legacy_parsing_or_exclusion_rules(self):
        parser = box.parser()
        # The launcher parser recognizes this start-only option globally;
        # ordinary command validation (not a filtered help parser) rejects it.
        args = parser.parse_args(
            ["--project=reset", "stop", "--import-session", "ses_demo"]
        )
        self.assertEqual((args.command, args.project), ("stop", Path("reset")))
        self.assertEqual(args.import_session, "ses_demo")
        for arguments in (
            ["start", "--new", "--resume"],
            ["sync", "--journald", "--no-journald"],
        ):
            with self.subTest(arguments=arguments):
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as error,
                ):
                    parser.parse_args(arguments)
                self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
