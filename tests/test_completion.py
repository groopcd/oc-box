import contextlib
import io
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from oc_box import (
    backups,
    dependencies,
    installation,
    oc_box as box,
    vault,
    workspace_service,
)

APP = Path(__file__).resolve().parents[1]
SCRIPT = APP / "completions/oc-box"
BASH = shutil.which("bash")


@unittest.skipUnless(BASH, "Bash is required for completion integration")
class BashCompletionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-completion-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def complete(self, *words, breaks=" \t\n\"'><=;|&(:", script=SCRIPT):
        assert BASH is not None
        command = r"""
source "$1"
COMP_WORDBREAKS=$2
shift 2
COMP_WORDS=(oc-box "$@")
COMP_CWORD=$((${#COMP_WORDS[@]}-1))
_oc_box
if ((${#COMPREPLY[@]})); then
    printf '%s\0' "${COMPREPLY[@]}"
fi
"""
        result = subprocess.run(
            [
                BASH,
                "--noprofile",
                "--norc",
                "-c",
                command,
                "completion-test",
                str(script),
                breaks,
                *words,
            ],
            cwd=self.root,
            env={"PATH": "/not-installed", "HOME": str(self.root), "LC_ALL": "C"},
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stderr, b"")
        return result.stdout.decode().rstrip("\0").split("\0") if result.stdout else []

    def test_commands_options_and_enums_track_all_cli_parsers(self):
        commands = next(
            action.choices
            for action in box.parser()._actions
            if action.dest == "command"
        )
        assert commands is not None
        root = self.complete("")
        self.assertEqual(
            {word for word in root if not word.startswith("-")}, set(commands)
        )
        main_options = {
            word for action in box.parser()._actions for word in action.option_strings
        }
        self.assertEqual(set(self.complete("-")), main_options)
        for name, parser in (
            ("backup", backups.parser_for("backup")),
            ("backup-info", backups.parser_for("backup-info")),
            ("restore", backups.parser_for("restore")),
            ("doctor", dependencies.parser()),
            ("web", workspace_service.web_parser()),
            ("completion", box.completion_parser()),
        ):
            with self.subTest(command=name):
                expected = {
                    word for action in parser._actions for word in action.option_strings
                }
                self.assertEqual(set(self.complete(name, "-")), expected)
        self.assertEqual(
            set(self.complete("--distro", "")), set(box.DISTROS) | set(box.ALIASES)
        )
        self.assertEqual(
            set(self.complete("doctor", "--feature", "")), set(dependencies.FEATURES)
        )
        credential_modes = next(
            action.choices
            for action in box.parser()._actions
            if action.dest == "credential_mode"
        )
        self.assertEqual(
            set(self.complete("credentials", "--credential-mode", "")),
            set(credential_modes),
        )
        actions = next(
            action.choices
            for action in box.secrets_parser()._actions
            if action.dest == "action"
        )
        assert actions is not None
        self.assertEqual(
            {word for word in self.complete("secrets", "") if not word.startswith("-")},
            set(actions),
        )

    def test_shared_parser_command_option_subsets_include_supported_aliases(self):
        # These commands share a parser, not the same supported option set.
        selectors = "project workspace"
        launch = (
            "distro new resume model cpus memory pids_limit device podman_arg "
            "clear_devices clear_podman_args no_vault"
        )
        actions = {action.dest: action for action in box.parser()._actions}
        for command, destinations in (
            ("start", f"{selectors} {launch} import_session"),
            ("reset", f"{selectors} {launch}"),
            ("stop", selectors),
            ("sync", selectors),
            ("repair", selectors),
            ("terminal", f"{selectors} maintenance"),
            ("upgrade", "check auto_update"),
            (
                "credentials",
                f"{selectors} credential_mode env_secret vault_secret clear_credentials",
            ),
            ("purge", f"{selectors} delete_sessions all yes"),
            ("list", ""),
            ("distros", ""),
            ("status", f"{selectors} json"),
            ("--status", f"{selectors} json"),
        ):
            with self.subTest(command=command):
                expected = {
                    option
                    for destination in ("help " + destinations).split()
                    for option in actions[destination].option_strings
                }
                self.assertEqual(set(self.complete(command, "-")), expected)

    def test_reset_session_selection_flags_match_shared_parser(self):
        for name, flag, destination in (
            ("new-conversation", "--new", "new"),
            ("resume-conversation", "--resume", "resume"),
        ):
            with self.subTest(case=name):
                args = box.parser().parse_args(["reset", flag])
                self.assertEqual(args.command, "reset")
                self.assertTrue(getattr(args, destination))
                self.assertEqual(self.complete("reset", flag), [flag])

    def test_options_are_scoped_to_the_command_or_vault_action(self):
        self.assertEqual(self.complete("backup", "--p"), ["--project", "--portable"])
        self.assertEqual(self.complete("upgrade", "--a"), ["--auto-update"])
        self.assertEqual(self.complete("doctor", "--p"), [])
        self.assertEqual(self.complete("--status", "--j"), ["--json"])
        self.assertEqual(self.complete("--status", "--n"), [])
        self.assertEqual(self.complete("sync", "--im"), [])
        self.assertEqual(self.complete("reset", "--im"), [])
        self.assertEqual(self.complete("secrets", "set", "--min"), [])
        self.assertEqual(
            self.complete("secrets", "passwd", "--min"), ["--min-passphrase-length"]
        )
        self.assertEqual(
            self.complete("secrets", "init", "--min-passphrase-length", "8", "--min"),
            ["--min-passphrase-length"],
        )
        self.assertEqual(self.complete("completion", "ba"), ["bash"])
        self.assertEqual(self.complete("completion", "bash", "ba"), [])

    def test_no_vault_is_offered_only_for_owning_launch_commands(self):
        self.assertEqual(self.complete("--no-v"), ["--no-vault"])
        for command in ("start", "reset", "web"):
            with self.subTest(command=command):
                self.assertEqual(self.complete(command, "--no-v"), ["--no-vault"])
        for command in (
            "stop",
            "sync",
            "repair",
            "terminal",
            "upgrade",
            "secrets",
            "credentials",
            "backup",
            "backup-info",
            "restore",
            "purge",
            "list",
            "distros",
            "status",
            "doctor",
            "completion",
            "--status",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.complete(command, "--no-v"), [])

    def test_vault_mode_completes_with_separate_and_equals_values(self):
        self.assertEqual(
            self.complete("credentials", "--credential-mode", ""),
            ["env", "selected", "vault"],
        )
        for words in (
            ("credentials", "--credential-mode", "va"),
            ("credentials", "--credential-mode", "=", "va"),
            ("credentials", "--credential-mode=va"),
        ):
            with self.subTest(words=words):
                self.assertEqual(self.complete(*words), ["vault"])
        self.assertEqual(
            self.complete("credentials", "--credential-mode=va", breaks=" "),
            ["--credential-mode=vault"],
        )

    def test_separate_and_equals_option_values_complete_with_default_word_breaks(self):
        for words in (
            ("--distro", "ub"),
            ("--distro", "=", "ub"),
            ("--distro=ub",),
        ):
            with self.subTest(words=words):
                self.assertEqual(self.complete(*words), ["ubuntu"])
        self.assertIn("ubuntu", self.complete("--distro", "="))
        self.assertEqual(self.complete("--distro=ub", breaks=" "), ["--distro=ubuntu"])
        self.assertEqual(
            self.complete("doctor", "--feature=back", breaks=" "),
            ["--feature=backup", "--feature=backup-info"],
        )
        self.assertEqual(self.complete("upgrade", "--auto-update", "o"), ["on", "off"])
        self.assertEqual(
            self.complete("credentials", "--credential-mode=sel"), ["selected"]
        )

    def test_option_arguments_named_like_commands_do_not_change_context(self):
        self.assertEqual(
            self.complete("--project", "restore", "backup", "--p"),
            ["--project", "--portable"],
        )
        self.assertEqual(
            self.complete("--project", "=", "backup", "upgrade", "--c"), ["--check"]
        )
        self.assertEqual(
            self.complete("--model=restore", "doctor", "--f"), ["--feature"]
        )

    def test_directory_file_and_archive_paths_preserve_spaces_and_metacharacters(self):
        directory = self.root / "project with spaces"
        directory.mkdir()
        archive = self.root / "backup with spaces.ocbox"
        archive.touch()
        literal = self.root / "$(touch NOT_EXECUTED).ocbox"
        literal.touch()
        self.assertEqual(
            self.complete("start", "--project", "project"), ["project with spaces/"]
        )
        self.assertEqual(
            self.complete("--project=project", breaks=" "),
            ["--project=project with spaces/"],
        )
        self.assertEqual(self.complete("--project", "backup"), [])
        self.assertEqual(self.complete("backup-info", "backup"), [archive.name])
        self.assertEqual(
            self.complete("restore", "--identity", "backup"), [archive.name]
        )
        self.assertEqual(self.complete("backup", "--output", "backup"), [archive.name])
        self.assertEqual(self.complete("restore", "$("), [literal.name])
        self.assertFalse((self.root / "NOT_EXECUTED").exists())
        self.assertEqual(
            self.complete("--project", "~/project"), [str(directory) + "/"]
        )
        self.assertEqual(
            self.complete("restore", "backup with spaces.ocbox", "backup"), []
        )
        self.assertEqual(self.complete("restore", "--", "backup"), [archive.name])
        self.assertEqual(self.complete("restore", "--", "--i"), [])

    def test_sensitive_or_open_ended_values_are_not_guessed_from_files_or_env(self):
        (self.root / "PRIVATE_TOKEN").touch()
        for words in (
            ("--workspace", ""),
            ("--import-session", ""),
            ("--model", ""),
            ("credentials", "--vault-secret", ""),
            ("credentials", "--env-secret", ""),
            ("secrets", "set", "PRIVATE"),
            ("backup", "--recipient", ""),
            ("backup", "--min-passphrase-length", ""),
            ("--podman-arg", ""),
        ):
            with self.subTest(words=words):
                self.assertEqual(self.complete(*words), [])

    def test_sourcing_registers_completion_without_optional_bash_completion_helpers(
        self,
    ):
        assert BASH is not None
        result = subprocess.run(
            [
                BASH,
                "--noprofile",
                "--norc",
                "-c",
                'source "$1"; complete -p oc-box',
                "test",
                str(SCRIPT),
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "complete -F _oc_box oc-box")

    def test_real_readline_tab_inserts_equals_values_and_quotes_space_paths(self):
        assert BASH is not None
        (self.root / "backup with spaces.ocbox").touch()
        (self.root / "project with spaces").mkdir()
        master, slave = pty.openpty()
        process = subprocess.Popen(
            [BASH, "--noprofile", "--norc", "-i"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=self.root,
            env={
                "PATH": "/not-installed",
                "HOME": str(self.root),
                "LC_ALL": "C",
                "TERM": "dumb",
                "HISTFILE": "/dev/null",
                "PS1": "",
            },
            start_new_session=True,
        )
        os.close(slave)
        output = bytearray()

        def wait_for(marker):
            deadline = time.monotonic() + 5
            while marker not in output:
                self.assertLess(time.monotonic(), deadline, bytes(output))
                if select.select([master], [], [], 0.1)[0]:
                    output.extend(os.read(master, 8192))

        try:
            setup = (
                "source "
                + shlex.quote(str(SCRIPT))
                + "; oc-box() { printf '<%s>' \"$@\"; printf '\\n'; }; printf 'READY\\n'\n"
            )
            os.write(master, setup.encode())
            wait_for(b"READY\r\n")
            for line, expected in (
                (b"oc-box --distro=ub\t\n", b"<--distro=ubuntu>\r\n"),
                (
                    b"oc-box backup-info back\t\n",
                    b"<backup-info><backup with spaces.ocbox>\r\n",
                ),
                (b"oc-box --project=proj\t\n", b"<--project=project with spaces/>\r\n"),
            ):
                os.write(master, line)
                wait_for(expected)
        finally:
            process.kill()
            process.wait(timeout=5)
            os.close(master)


class CompletionInstallationTests(unittest.TestCase):
    def test_reset_import_session_is_refused_before_host_lifecycle(self):
        with (
            patch.object(box, "check_backend") as backend,
            patch.object(box, "find_workspace") as workspace,
            patch.object(vault, "hidden") as prompt,
            self.assertRaisesRegex(
                box.BoxError, "^--import-session applies only to start\\.$"
            ),
        ):
            box.main(["reset", "--import-session", "ses_completion_fixture"])
        for mock in (backend, workspace, prompt):
            mock.assert_not_called()

    def test_print_completion_is_informational_and_does_not_require_host_tools_or_secrets(
        self,
    ):
        with (
            patch.object(box, "check_backend") as backend,
            patch.object(vault, "hidden") as prompt,
            patch.object(box, "workspace_paths") as workspaces,
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "1", "PATH": "/not-installed"}),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(box.main(["completion", "bash"]), 0)
        self.assertEqual(output.getvalue(), SCRIPT.read_text())
        for mock in (backend, prompt, workspaces):
            mock.assert_not_called()

    def test_source_install_stages_and_updates_completion_without_editing_bashrc(self):
        with tempfile.TemporaryDirectory(
            prefix="oc-box-completion-install-"
        ) as temporary:
            home = Path(temporary)
            profile = home / ".bashrc"
            profile.write_text("# user profile\n")
            with contextlib.redirect_stdout(io.StringIO()):
                first = installation.install(APP, home)
                second = installation.install(APP, home)
            self.assertEqual(
                (home / ".local/share/bash-completion/completions/oc-box").read_bytes(),
                SCRIPT.read_bytes(),
            )
            self.assertEqual(
                (first / "completions/oc-box").read_bytes(), SCRIPT.read_bytes()
            )
            self.assertEqual(
                (home / ".local/share/oc-box/completions").resolve(),
                second / "completions",
            )
            self.assertEqual(profile.read_text(), "# user profile\n")
