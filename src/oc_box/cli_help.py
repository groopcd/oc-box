"""Help views for the launcher without changing its shared argument grammar."""

import argparse
import sys


class _ImplicitCommand(str):
    """Keep the original default valid while distinguishing it from argv."""


DEFAULT_COMMAND = _ImplicitCommand("start")


_COMMAND_GROUPS = (
    (
        "Workspaces",
        (
            ("start", "Start or resume OpenCode (default)"),
            ("stop", "Stop a workspace container without removing it"),
            ("sync", "Archive workspace conversations to the host"),
            ("reset", "Rebuild the container OS, then launch OpenCode"),
            ("repair", "Repair the registered project directory identity"),
            ("purge", "Remove workspace resources"),
            (
                "preferences",
                "View or save file-open, import, and image-preview preferences",
            ),
        ),
    ),
    (
        "Interfaces and sharing",
        (
            ("terminal", "Open a session terminal or a maintenance shell"),
            ("web", "Open Studio with the native OpenCode browser interface"),
            ("tui", "Open the experimental workspace manager"),
            ("share", "Share a host file or directory read-only with a live workspace"),
            ("attach", "Attach a host image to a native OpenCode session"),
        ),
    ),
    (
        "Inspection and diagnostics",
        (
            ("list", "List registered workspaces"),
            ("status", "Report workspace resources and sessions"),
            ("logs", "Read launcher activity or follow environment preparation"),
            ("distros", "List supported container distributions"),
            ("doctor", "Check prerequisites and show installation guidance"),
            ("runtime-check", "Check an exact OpenCode 2.x release in isolation"),
        ),
    ),
    (
        "Credentials and backups",
        (
            ("credentials", "View or set workspace credential forwarding"),
            ("secrets", "Manage the encrypted host vault"),
            ("backup", "Create an encrypted workspace backup"),
            ("backup-info", "Inspect an encrypted workspace backup"),
            ("restore", "Restore a backup into a new workspace"),
        ),
    ),
    (
        "Installation",
        (
            ("upgrade", "Check for or install a launcher update"),
            ("--uninstall", "Review or remove the verified launcher installation"),
            ("completion", "Print the Bash completion script"),
        ),
    ),
)

# Keys select existing actions by dest or option string. Shared destinations
# include every spelling with that destination (for example, both log toggles).
_RESOURCE_KEYS = ("cpus", "memory", "pids_limit")
_OPTION_GROUPS = {
    "workspace": ("Workspace selection", ("project", "workspace")),
    "session": ("Session and model", ("new", "resume", "import_session", "model")),
    "container": (
        "Container setup and resources",
        (
            "distro",
            *_RESOURCE_KEYS,
            "device",
            "podman_arg",
            "clear_devices",
            "clear_podman_args",
        ),
    ),
    "sync-resources": ("Container resource limits", _RESOURCE_KEYS),
    "sync-mode": ("History source", ("persisted",)),
    "native": (
        "Native terminal UI",
        (
            "image_preview",
            "host_file_open",
            "host_drop",
            "host_clipboard",
            "host_clipboard_symlinks",
        ),
    ),
    "vault": ("Credential delivery", ("no_vault",)),
    "runtime-guard": (
        "Native runtime safeguard",
        ("runtime_guard", "convert_legacy_container", "rebuild_legacy_container"),
    ),
    "logging": (
        "Lifecycle logging",
        ("log_dir", "journald", "lifecycle_diagnostics", "verbose"),
    ),
    "terminal": ("Terminal mode", ("maintenance",)),
    "status": ("Output", ("json", "table")),
    "purge": ("Removal options", ("delete_sessions", "all", "yes", "discard_guarded")),
    "credentials": (
        "Credential policy",
        ("credential_mode", "env_secret", "vault_secret", "clear_credentials"),
    ),
    "release": (
        "Release selection",
        ("check", "update_channel", "upgrade_vault", "upgrade_tui"),
    ),
    "auto-update": ("Automatic updates", ("auto_update",)),
    "help": ("Help", ("-h",)),
}

_LAUNCH_GROUPS = ("workspace", "session", "container", "native", "vault", "logging")
_COMMAND_SCOPES = {
    "start": (*_LAUNCH_GROUPS, "runtime-guard"),
    "reset": _LAUNCH_GROUPS,
    "stop": ("workspace", "logging"),
    "sync": ("workspace", "sync-mode", "sync-resources", "logging"),
    "terminal": ("workspace", "terminal", "logging"),
    "repair": ("workspace",),
    "purge": ("workspace", "purge"),
    "credentials": ("workspace", "credentials"),
    "upgrade": ("release", "auto-update"),
    "status": ("workspace", "status"),
    "list": (),
    "distros": (),
}
_SCOPE_OMISSIONS = {"reset": ("import_session",)}

_WORKSPACE_NOTE = "With no workspace selector, use the current directory."
_LOGGING_NOTE = "Logging options apply to this invocation only."
_LAUNCH_REQUIREMENTS = "Run on the Linux host as a regular user with rootless Podman and native OpenCode installed."
_COMMAND_NOTES = {
    "start": (
        "This is the default command.",
        _LAUNCH_REQUIREMENTS,
        _WORKSPACE_NOTE,
        "Use 'oc-box reset' to change settings for an existing container.",
        _LOGGING_NOTE,
    ),
    "reset": (
        _LAUNCH_REQUIREMENTS,
        _WORKSPACE_NOTE,
        "Rebuilding retains the project, private home, and session history.",
        "Exit the owning client first. System packages installed in the old container must be recreated.",
        _LOGGING_NOTE,
    ),
    "stop": (_WORKSPACE_NOTE, _LOGGING_NOTE),
    "sync": (
        _WORKSPACE_NOTE,
        "Exports workspace conversations to the host and stops the container afterward; it may prepare or recreate the environment.",
        "Use --persisted for a guarded snapshot without normal preparation or container recreation.",
        (
            "Set resource limits only when recreating a missing container for a "
            "registered workspace; use reset for an existing container."
        ),
        _LOGGING_NOTE,
    ),
    "terminal": (
        _WORKSPACE_NOTE,
        "Requires an interactive host terminal. The V2 session shell needs an existing private owner; OpenCode 1.x only supports the maintenance shell.",
        _LOGGING_NOTE,
    ),
    "repair": (
        _WORKSPACE_NOTE,
        "Repair the original registered directory and its marker; never overwrite another workspace's marker.",
    ),
    "purge": (
        _WORKSPACE_NOTE,
        "Without deletion flags, keep project files and host session archives.",
        "Guarded disposal requires the full workspace ID and typed terminal confirmation.",
    ),
    "credentials": (
        _WORKSPACE_NOTE,
        "Without policy options, show the selected or default forwarding policy and credential names only.",
        "Global vault mode does not take a name list; use --clear-credentials when switching from named selections.",
    ),
    "upgrade": (
        "Run on the host as a regular user. Updates require a supported launcher installation.",
        "Successful explicit channel choices are remembered; previews do not save them.",
        "Use --auto-update separately from release selection options.",
    ),
    "status": (
        "'oc-box --status' is equivalent.",
        "With no workspace selector, report all registered workspaces.",
    ),
    "list": (
        "Lists every registration; STATUS describes project or marker health, not container state.",
    ),
    "distros": (
        "Shows supported container image references and marks the host default.",
    ),
}
_COMMAND_EXAMPLES = {
    "start": "oc-box start --project ~/devel/demo --new",
    "reset": "oc-box reset --project ~/devel/demo --cpus 8",
    "stop": "oc-box stop --project ~/devel/demo",
    "sync": "oc-box sync --project ~/devel/demo",
    "terminal": "oc-box terminal --maintenance --project ~/devel/demo",
    "repair": "oc-box repair --workspace WORKSPACE_ID",
    "purge": "oc-box purge --workspace WORKSPACE_ID",
    "credentials": "oc-box credentials --project ~/devel/demo",
    "upgrade": "oc-box upgrade --devel --check",
    "status": "oc-box status --project ~/devel/demo --table",
}
_COMMAND_DESCRIPTIONS = {
    name: description
    for _, commands in _COMMAND_GROUPS
    for name, description in commands
}


class _LauncherHelpAction(argparse.Action):
    def __init__(
        self,
        option_strings,
        dest=argparse.SUPPRESS,
        default=argparse.SUPPRESS,
        help=None,
    ):
        super().__init__(option_strings, dest, nargs=0, default=default, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        command = getattr(namespace, "command", None)
        if command is DEFAULT_COMMAND:
            command = None
        if command in (None, "start") and getattr(namespace, "status", False):
            command = "status"
        if command is None:
            parser.print_help()
        else:
            print(parser.format_command_help(command), end="")
        parser.exit()


class LauncherParser(argparse.ArgumentParser):
    """Keep the launcher grammar, but scope help to an explicitly parsed command."""

    def __init__(self, *args, **kwargs):
        kwargs["add_help"] = False
        super().__init__(*args, **kwargs)
        self.add_argument(
            "-h",
            "--help",
            action=_LauncherHelpAction,
            dest=argparse.SUPPRESS,
            help="show this help message and exit",
        )

    def parse_known_args(self, args=None, namespace=None):
        if args is None:
            args = sys.argv[1:]
        args = list(args)
        # A bare verbosity flag must not consume a following launcher command.
        # The optional level can always be supplied explicitly with '='.
        for index, arg in enumerate(args):
            if arg == "--":
                break
            if arg in ("--ve", "--ver", "--vers"):
                args[index] = "--version"
            elif arg.partition("=")[0] == "--t":
                # This spelling selected upgrade --tui before --table existed.
                args[index] = "--tui" + arg[len("--t") :]
            elif (
                arg in ("--verb", "--verbo", "--verbos", "--verbose")
                and index + 1 < len(args)
                and args[index + 1] in _COMMAND_DESCRIPTIONS
            ):
                args[index] = "--verbose=all"
        namespace, extras = super().parse_known_args(args, namespace)
        if getattr(namespace, "command", None) is DEFAULT_COMMAND:
            namespace.command = "start"
        return namespace, extras

    def _actions_for(self, keys, omit=()):
        return [
            action
            for key in keys
            if key not in omit
            for action in self._actions
            if action.help is not argparse.SUPPRESS
            and (key == action.dest or key in action.option_strings)
        ]

    @staticmethod
    def _add_help_group(formatter, title, actions):
        if actions:
            formatter.start_section(title)
            formatter.add_arguments(actions)
            formatter.end_section()

    def format_help(self):
        formatter = self._get_formatter()
        formatter.add_usage("%(prog)s [COMMAND] [OPTIONS]", (), ())
        formatter.add_text(self.description)
        for title, commands in _COMMAND_GROUPS:
            # Presentation-only positionals align and wrap the command index.
            rows = [
                argparse.Action((), name, help=description)
                for name, description in commands
            ]
            self._add_help_group(formatter, title, rows)
        self._add_help_group(
            formatter,
            "Informational options",
            self._actions_for(("-h", "about", "version", "status")),
        )
        formatter.add_text(
            "Run '%(prog)s COMMAND --help' for command options. "
            "Without a command, '%(prog)s' defaults to start."
        )
        return formatter.format_help()

    def _command_sections(self, command):
        omit = _SCOPE_OMISSIONS.get(command, ())
        return [
            (title, self._actions_for(keys, omit))
            for title, keys in (
                _OPTION_GROUPS[name] for name in (*_COMMAND_SCOPES[command], "help")
            )
        ]

    def command_option_strings(self, command: str) -> set[str]:
        """Return the visible launcher flags, or an empty set for dedicated commands."""
        if command not in _COMMAND_SCOPES:
            return set()
        return {
            option
            for _, actions in self._command_sections(command)
            for action in actions
            for option in action.option_strings
        }

    def format_command_help(self, command: str) -> str:
        formatter = self.formatter_class(prog=f"{self.prog} {command}")
        if command not in _COMMAND_SCOPES:
            # Dedicated parsers handle command-first help. Do not dispatch or
            # import them if the launcher saw a legacy selector-first spelling.
            formatter.add_text(
                f"Put the command first for its help: '{self.prog} {command} --help'."
            )
            return formatter.format_help()

        sections = self._command_sections(command)
        actions = [action for _, group_actions in sections for action in group_actions]
        # Keep small usages explicit, but list longer sets of flags just once,
        # in their respective sections.
        usage = "%(prog)s [OPTIONS]" if len(actions) > 6 else None
        formatter.add_usage(usage, actions, self._mutually_exclusive_groups)
        formatter.add_text(_COMMAND_DESCRIPTIONS[command])
        for title, group_actions in sections:
            self._add_help_group(formatter, title, group_actions)
        formatter.add_text(" ".join(_COMMAND_NOTES.get(command, ())))
        if command in _COMMAND_EXAMPLES:
            formatter.add_text("Example: " + _COMMAND_EXAMPLES[command])
        return formatter.format_help()
