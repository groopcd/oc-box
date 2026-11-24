# oc-box command reference

## Name

oc-box - persistent rootless Podman environments for mainline OpenCode.

## Synopsis

```text
oc-box [start] [--project PATH | --workspace ID] [START-OPTIONS] [LIFECYCLE-LOG-OPTIONS]
oc-box stop [--project PATH | --workspace ID] [LIFECYCLE-LOG-OPTIONS]
oc-box sync [--project PATH | --workspace ID] [--cpus N] [--memory SIZE] [--pids-limit N] [LIFECYCLE-LOG-OPTIONS]
oc-box reset [--project PATH | --workspace ID] [RESET-OPTIONS] [LIFECYCLE-LOG-OPTIONS]
oc-box repair [--project PATH | --workspace ID]
oc-box terminal [--project PATH | --workspace ID] [--maintenance] [LIFECYCLE-LOG-OPTIONS]
oc-box share PATH [--project PATH | --workspace ID] [--acknowledge-risk]
oc-box attach PATH --session ID [--project PATH | --workspace ID] [--text TEXT] [--send] [--acknowledge-risk]
oc-box attach --broker --session ID [--project PATH | --workspace ID]
oc-box preferences [--project PATH | --workspace ID] [--host-drop on|off] [--host-clipboard on|off] [--host-clipboard-symlinks on|off] [--host-file-open on|off] [--image-preview on|off] [--startup-logo ascii|graphics] [--json | --table]
oc-box purge [--project PATH | --workspace ID] [--delete-sessions | --all] [-y]
oc-box list
oc-box distros
oc-box doctor [--json | --table] [--feature FEATURE]...
oc-box completion bash
oc-box web [--project PATH | --workspace ID] [--session ID] [--port N] [--no-open] [--no-vault] [--attachments] [--workspace-layout] [LIFECYCLE-LOG-OPTIONS]
oc-box --status [--project PATH | --workspace ID] [--json | --table]
oc-box status [--project PATH | --workspace ID] [--json | --table]
oc-box logs [--journald | --log-dir PATH] [--project PATH | --workspace ID] [--level LEVEL | --verbose[=LEVEL]] [--limit N] [--tail] [--follow] [--json | --table]
oc-box logs [--log-dir PATH] --path
oc-box tui [--project PATH | --workspace ID] [--read-only] [--demo]
oc-box upgrade [--check] [--stable | --devel] [--vault] [--tui]
oc-box upgrade --auto-update on|off
oc-box secrets init|passwd [--min-passphrase-length N]
oc-box secrets list
oc-box secrets set|remove NAME
oc-box credentials [--project PATH | --workspace ID] [CREDENTIAL-OPTIONS]
oc-box backup [--project PATH | --workspace ID] --output FILE [--portable] [--host [--opencode-auth-export FILE|-]] [--recipient AGE_KEY]...
oc-box backup [--project PATH | --workspace ID] --output FILE [--portable] [--host [--opencode-auth-export FILE|-]] [--min-passphrase-length N]
oc-box backup-info FILE [--identity FILE]... [--json | --table]
oc-box restore FILE --project NEW_PATH [--host [--include-opencode]] [--identity FILE]...
oc-box restore FILE --host [--include-opencode] [--identity FILE]...
oc-box --about
oc-box --version
oc-box -h | --help
oc-box COMMAND --help
```

The command defaults to `start`. For the shared launcher commands, such as
`start`, `reset`, `stop`, `sync` and `terminal`, options may appear before or
after the command. Put dedicated commands (`attach`, `backup`, `backup-info`,
`completion`, `doctor`, `logs`, `preferences`, `restore`, `secrets`, `share`,
`tui` and `web`) first. Run the launcher on the host as your regular user,
without sudo. It manages rootless Podman; container root is mapped to the
unprivileged host user.

`START-OPTIONS` cover session selection, model, container setup, native TUI
features and credential delivery. `RESET-OPTIONS` use the corresponding reset
options but do not import a host session. See [start](#start) and [reset](#reset)
for their effects, or `oc-box start --help` and `oc-box reset --help` for the
scoped flags. `CREDENTIAL-OPTIONS` are `--credential-mode`, `--env-secret`,
`--vault-secret` and `--clear-credentials`; see [credentials](#credentials).

`LIFECYCLE-LOG-OPTIONS` are `[--journald | --no-journald] [--log-dir PATH]
[--lifecycle-diagnostics] [--verbose[=LEVEL]]`. They apply only to the current invocation; see
the [activity journal settings](#dashboard-state-and-activity-journal).

## Description

oc-box starts unmodified OpenCode and its local MCP servers, plugins,
formatters, shell, and file tools together inside a persistent
development container. It associates one environment with one canonical project
directory. The entire project, including hidden files, Git history, untracked
files, and unfinished edits, is mounted writable at the same absolute path.

The container HOME has the host user's home pathname but uses a private named
volume. It is not a mount of the host home. The live OpenCode database, session
IDs, snapshots, caches, and home-installed dependencies persist in this volume.
Packages installed elsewhere in the OS persist in the container's writable
layer until that container is reset or removed.

On normal exit, oc-box exports conversation history to separate archive
sessions in the normal host OpenCode database, then stops the container.
Launching oc-box again restores the native terminal's saved open-session set
when supported; closing a tab does not delete its synchronized history. Running
processes do not survive a stop. Host OpenCode and its configuration are
unchanged. On V2, ordinary tool permission asks for the actual container Build
agent are auto-allowed; explicit session/mandatory-policy denials remain denied,
and Plan/subagents retain their own rules. Copied ordinary global/Build-agent
rules are overridden by the existing final Build wildcard, as before. V1's
existing Build permission behavior is unchanged.

The default resource limits are 4 CPU cores, 8 GiB RAM, and 2048 processes.
These are ceilings, not reservations: resources are consumed dynamically up to
those limits, and OpenCode itself has no fixed 4/8 GiB minimum requirement.
There is no disk quota. Containers share the host kernel and can reach network
services, including configured host services at `host.containers.internal`.
The project is intentionally writable and can be deleted by container commands.
Additional Podman options and devices explicitly extend these defaults.

### Lifecycle output

Startup and shutdown are quiet by default. On an interactive terminal, oc-box
shows transient stage progress on one line and clears it instead of leaving a
transcript of each step. Noninteractive output, including pipes and `TERM=dumb`,
does not get routine stage start/done lines. Actionable warnings, errors and
confirmation or credential prompts remain visible. Native OpenCode output and
the native terminal and browser UIs are not changed. During a cold start or
reset, image fetching and package installation show one short line for each
task instead of printing the installers' output. Use `oc-box logs --tail`
to see which tasks started, their results, and how long they took, or add
`--follow` to watch from another terminal. If you used a one-time `--log-dir`,
use that same directory when reading the log. Native host drops and
clipboard importing print one short line each when enabled. Use
`oc-box start --verbose` for the full behavior, limits and clipboard symlink policy.

After a history sync or recovery attempt, oc-box reports one final history
result: saved normally, recovered after stop, partial, failed, empty, or cleanup
unverified. The recovery result itself carries the interruption and
unconfirmed-background-completion caution; archived history does not prove
work finished. Incomplete results retain guidance for retrying
`oc-box sync` when appropriate. The presentation does not change lifecycle steps,
recovery eligibility, history verification or exit codes.

Use `oc-box logs --verbose=ALL` to inspect all retained activity, or
`oc-box start --verbose` to show live installer output and launcher diagnostics
for one launch. See [Launcher diagnostics](#launcher-diagnostics) for
`--lifecycle-diagnostics`.

### OpenCode versions

**OpenCode 2.x is the primary documentation path.** OpenCode 1.x remains supported
as a **legacy** line, with [separate operating guidance](legacy/README.md).
The [documentation hub](README.md) and task guides focus on 2.x. This complete
reference retains both versions' conditional rules so legacy behavior, history
preservation and downgrade constraints remain discoverable.

oc-box 1.7 retains OpenCode 1.x and 2.x support, tested with 1.19.5 and
2.2.6. The host release selects the exact container package: `opencode-ai` for
1.x, `@opencode/cli` for 2.x. V2 uses a workspace-owned private stdio server with
native `--server` clients and current credentials at each new owning startup.
Without an explicit workspace policy, V2 defaults to all current global vault
entries when the host vault is present; V1 retains legacy env-file forwarding.
Isolated setup/history probes use native OpenCode `--standalone`, not an oc-box
flag. The host shared service is never restarted or exposed inside the container.
Its service settings are not copied. V2 uses a separate container database/resume
state seeded from a consistent V1 snapshot
and migrated by upstream; V1 history is retained for downgrade.

Native V2 plugins use the new API; copied V1 local plugins get bounded adapters
for shell environment, tool execution/output/definition, text custom tools and
provider discovery. Unsupported hooks still require native ports. Supported
legacy config and V2 fields are retained; the Build plugin is version-aware.
MCP Code Mode and native formatting are preserved. Upstream 2.2.6 does not run language servers
or expose LSP diagnostics; native LSP remains on 1.x. See `docs/opencode-2.md`
for environment strategy, migration limits, and accepted host validation.

## Commands

### completion

Print the bundled shell completion script to stdout. Bash is currently the only
supported shell: `oc-box completion bash`. This informational command needs no
Podman, OpenCode, network connection, host credential access, or workspace and
can run inside the container. `oc-box completion --help` describes the syntax.
See Bash completion under Installation and manuals for activation and install
paths. It never edits shell startup files automatically.

```bash
source <(oc-box completion bash)
```

### doctor

Report prerequisites by feature, missing commands, and installation commands for
the OS running doctor. It is read-only: no packages installed, network requests,
Podman operations, workspace/config changes, or secret prompts. Run on the host
for host advice. Inside oc-box it explicitly reports `current-container`; this
cannot inspect host packages. A workspace's selected distro does not determine
host package guidance. Unknown hosts list required providers without guessing a
package manager from the default workspace image or executing os-release text.

```sh
oc-box doctor
oc-box doctor --feature private-output
oc-box doctor --feature backup --feature vault
oc-box doctor --feature backup-info --json
oc-box doctor --feature backup-info --table
```

Feature selectors: `launch`, `private-output`, `maintenance`, `history`, `backup`, `backup-info`, `restore`,
`vault`, `github-upgrade`, `bootstrap`, `storage`, `overlay`, `manuals`, and
`docs-build`. Without selectors all are reported, but exit status checks only
core `launch` prerequisites. With selectors, exit 0 means every selected
feature's prerequisites are present; exit 1 means at least one is missing or
incompatible. JSON schema version 1 includes scope, detected distro, feature
availability, dependency details, missing providers, package names, installation
commands, and notes. Missing optional features never disable unrelated commands.
`--table` shows feature readiness, missing prerequisites and package guidance
in columns, without changing the checks or exit status.
Feature commands check their prerequisites before secret prompts or destructive
workspace changes and include OS-specific fixes.

This checks installed prerequisites, not full runtime certification. Podman
still checks rootless mode, subordinate IDs, controller delegation, and mounts;
interactive commands check terminal requirements. Command presence does not
establish a compatible version or validate every backend/driver configuration.
Normal OpenCode launch verifies its host/container versions separately.

`private-output` probes actual Linux anonymous-memory file creation using the
same native-Python/libc path as credential capture and backup/restore. V2 owning
launches require it even with `--no-vault`; ordinary V1 launches without vault
values and attach-only clients do not. It is separate from the version-neutral
`launch` prerequisites: doctor does not start OpenCode to infer its version.

### start

Create or resume an environment. This is the default when no command is given.
The project directory must already exist and be dedicated to the project.
Without a selector, use the current directory. With `--workspace`, use the
registered directory; a workspace ID takes precedence if both selectors are
provided. Do not combine selectors when a single selector will do.

On first launch, detect the host distro, create a labeled container and private
home volume, install baseline tools, install the exact host OpenCode release,
copy configuration, resolve plugin dependencies, and start a conversation.
Afterward, start the existing environment. V2 startup waits for the required
Build permissions plugin to actually activate, not just for the server to
answer. If it cannot activate, oc-box refuses to start the client.

Native V2 terminal clients preserve their open-tab membership independently of
history synchronization: a closed tab stays closed, while its transcript and
child sessions remain in the history
inventory. If every tab was closed, the next default terminal launch opens native
home without creating or reopening a conversation. Use native
Ctrl+P → Switch session to reopen one. OpenCode records tab membership/order, not
the exact focused tab; oc-box chooses only a verified open, unarchived root.

This behavior uses the audited native 2.2.6/2.2.7/2.3.0 storage contract.
Without saved native state for the current scope, V1 and legacy/first V2 launches
retain latest-root selection. Explicit owning-launch `--resume`, import and model
selections retain their deliberate selection behavior; `--new` starts another
conversation in the same environment. Additional clients use only the existing
owner's verified open default or native home, never a new historical selection.
Archived roots remain synchronized but are never chosen automatically.

```sh
mkdir -p ~/devel/demo
oc-box --project ~/devel/demo
oc-box start --project ~/devel/demo --new
oc-box --project ~/devel/demo --image-preview
oc-box --project ~/devel/demo --host-drop
```

For V2, initialize and populate the host vault **before the first project launch**
to use the global default immediately; see secrets below for installation and
masked input. No per-project credential registration is needed. Each new owner
unlocks once and delivers all current entries unless an explicit env/selected
policy overrides it. Changes to vault values/names apply on the next new owner,
not to attached clients. With no vault, env-file fallback remains available with
concise setup guidance. Native OpenCode saved/OAuth credential refresh/copy
remains independent in every mode. `--no-vault` opts
out for one owning V2 launch without saving policy. Configured vault failures
abort before setup; they do not silently fall back to the environment.

Cold startup includes image/package/plugin downloads; warm launches retain
compatible caches. See [Lifecycle output](#lifecycle-output) for transient
progress and the final history result. The progress display distinguishes
waiting for live activity to settle from copying history archives. A separate
loading line covers blank TUI startup frames and disappears when OpenCode draws
text. By default, supported terminals show a tiny oc-box cube PNG and other
terminals use the `[/]` ASCII mark. Save `oc-box preferences --startup-logo ascii`
for an existing workspace to use the ASCII mark even on a supported terminal.
The launcher removes the image before forwarding native output, so it may
appear only briefly.
Pipes and `TERM=dumb` get neither.
The container OpenCode version is verified against the host on every launch;
exact upgrades and downgrades are supported, with no fallback to another release.
With a V2 release supporting image previews, `--image-preview` enables prompt
and transcript image previews through an ephemeral native-CLI overlay for this
owning TUI generation, without replacing saved private preferences. See the
option below for compatibility and lifetime.

Native V2 terminal preferences are container-local. Host `cli.json` seeds an
absent private file, but later config refreshes preserve the container's existing
file, including explicit permission, notification, theme and tab-scope choices.
Use Ctrl+P → Open settings inside that container to change them. Normal server
configuration, authentication and plugin refreshes remain separate. oc-box does
not force autoaccept or copy the container's preferences back to the host.

An owner may be ready at native home without a default conversation. Additional
terminal TUIs preserve that state instead of forcing a historical root. Default
session shells need an eligible native session. The browser can still pair and
show native Home, or select an existing session with `web --session ID`.
The browser image-drop adapter has its own target checks; pairing alone does not
authorize a file upload. Location-scoped sharing and metadata browsing do not
invent a session.

Experimental outside-host image/PDF drops into the native TUI are enabled by
default for supported clients. Save `--no-host-drop` to disable importing for
this workspace, or `--host-drop` to enable it again. A new additional
`start` TUI client reads the preference without restarting the existing owner;
existing clients do not live-toggle. This is separate from
the `attach` confirmation broker and from browser uploads; see the option below
for the host-input authorization boundary and terminal requirements.

Host clipboard imports are separately **off by default**. Save `--host-clipboard`
to enable the Ctrl+Y import shortcut for this and future workspace TUI clients,
or `--no-host-clipboard` to disable it. Only that shortcut reads the clipboard;
enabling the preference or starting a client does not read it.

### preferences

Read or change host-only UI preferences for an **existing registered workspace**
without opening a client, starting/stopping a container, refreshing native
configuration or unlocking credentials:

```sh
oc-box preferences --project ~/devel/demo
oc-box preferences --project ~/devel/demo --host-drop off
oc-box preferences --workspace WORKSPACE_ID --host-drop on
oc-box preferences --project ~/devel/demo --host-clipboard on
oc-box preferences --project ~/devel/demo --host-clipboard-symlinks off
oc-box preferences --project ~/devel/demo --host-clipboard off
oc-box preferences --project ~/devel/demo --host-file-open on
oc-box preferences --project ~/devel/demo --image-preview on
oc-box preferences --project ~/devel/demo --startup-logo graphics
oc-box preferences --project ~/devel/demo --json
oc-box preferences --project ~/devel/demo --table
```

`--project` and `--workspace` are mutually exclusive. With no change option this
is read-only. The independent settings accept `on`/`off` except `--startup-logo`:

| Setting | Default when no choice is saved |
| --- | --- |
| `--host-drop` | `on`: pathname/file-drop imports on supported clients |
| `--host-clipboard` | `off`: no clipboard shortcut |
| `--host-clipboard-symlinks` | `on`: follow ordinary links for clipboard file imports |
| `--host-file-open` | Follow the verified installation policy for Ctrl-click host opening |
| `--image-preview` | `off`: no launcher image-preview override; keep native settings |
| `--startup-logo ascii\|graphics` | `graphics`: cube PNG where supported; otherwise `[/]` |

The symlink preference can be saved independently, even while clipboard
importing is off; it takes effect on a later enabled client. The strict symlink
choice remains saved when clipboard importing is later disabled, so re-enabling
does not silently restore link following.
This command also works without a TTY. It does not discover desktop helpers or
read the clipboard, even when saving an enable choice. Host opening resolves
the current launch flag first, then the saved workspace choice, then the
installation policy. An image-preview preference applies only on the next
owning native V2 start/reset; additional clients inherit their running owner's
setting, not a preference changed after that owner started.
JSON output includes the effective `host_clipboard` and
`host_clipboard_symlinks` booleans. It includes `host_file_open` and
`image_preview` and `startup_logo` only when explicitly saved; absence means use their default
rules. `source` still describes the preference file, not each field. Use
`--table` to see the choices and which are saved, default, or inherited.
These are requests for future clients, not a live availability check. The
presentation does not save a preference by itself.

Corrupt, unsafe or mismatched preferences refuse rather than silently granting
access. Changes apply to future clients only. Preferences survive ordinary
stop/start and reset in a private host `ui-preferences.json` beside workspace metadata,
with an independent lock so an active owning launcher need not exit.
Only policy booleans and the startup logo choice are saved, never clipboard
contents or a helper path.
They are not native CLI settings, credential policy or archived authority:
backup/restore into a fresh workspace does not reactivate these host preferences.
The new workspace uses the defaults above until explicitly changed.

### stop

Stop a registered container without deleting the project, container OS, private
home, session IDs, or metadata. Already stopped or missing containers need no
start. This command can run from a second host terminal while OpenCode is active.
Its processes stop, and the active client may disconnect or report tool errors.
Exit that launcher before starting another instance or resetting the workspace.
Use `sync` afterward if a final history export was missed.

For V2, this command records stop intent before waiting for a private transition
lock (up to 45 seconds). A same-version owning launcher then suppresses automatic
post-stop history recovery rather than undoing the successful stop. A start whose
engine outcome could not be verified leaves a private uncertainty fence: stop is
still attempted, but no successful-stop message is printed and the command returns
nonzero. Do not delete control files to bypass that refusal; verify the host state
before repairing the control condition. Older launchers or direct Podman commands
do not participate in this coordination protocol. V1 stop behavior is unchanged.

```sh
oc-box stop --project ~/devel/demo
```

### share

**Experimental, V2 only:** temporarily give a running workspace live read access
to a specific host file or folder through native OpenCode tools. Run this from
a second **host** terminal. No container recreation, filesystem mount, host
listener, project staging copy, or saved MCP/configuration entry is needed.
It does not start a workspace, unlock the vault, or refresh credentials.
Put `share` first, then its path and workspace selector.

```sh
oc-box share ~/Pictures/Screenshots --project ~/devel/demo
oc-box share ~/Pictures/example.png --workspace 0123456789abcdef0123456789abcdef
```

The host prints the selected path and a security warning. Type `share` to
confirm. For explicitly authorized noninteractive use, `--acknowledge-risk`
replaces that confirmation, **not** the warning. There is no blanket `--yes`.
Choose the narrowest folder or single file needed; hidden files and newly added
files in a shared folder are included. Sharing the whole home, broad system
directories, kernel/device/runtime paths, oc-box application/state/vault paths,
host OpenCode config/data, SSH keys, or GnuPG directories is refused.

Keep the command open while using the share. It prints a unique MCP server name
such as `oc_box_share_<random-id>`. Ask OpenCode to use that server's
`list_directory`, `read_file`, or `read_image` tools. Paths are relative to the
selected root; `.` means the root, including a single shared file. Text reads
require UTF-8 and are limited to 1 MiB. Image reads return PNG/JPEG/GIF/WebP image
content, limited to 8 MiB. Directory listings include at most 1000 entries;
larger directories require a narrower selection. Symlinks, hard-linked files,
special files, parent traversal and nested mounts are not followed. Directory
sharing requires Linux `openat2` support (kernel 5.6 or newer, subject to host
policy); unsupported resolution fails closed.

Reads see current file contents and additions/deletions in the selected folder,
including atomic replacement of its children. Replacing, moving, or removing
the **selected root itself** requires sharing it again explicitly. Reads are not
an atomic folder snapshot; a changing file can be refused for retry. This is
tool access, **not** a normal container path: shell commands and native file
tools cannot use it as a mount. No write, command-execution or clipboard tool is
provided, and existing native permissions are not changed.

Ctrl-C closes sharing and removes its native client-MCP tools. A workspace stop,
service disconnect or reload ends the connection; no automatic reconnect or
regrant occurs. The command holds an explicit client lease, so the owner waits
for it before normal synchronization/stop. Close the sharing command when done
or use the normal explicit `oc-box stop`. Separate shares can run in separate
host terminals. Existing containers need no preparation or reset.

**Disclosure warning:** read-only does not mean confidential. OpenCode can send
shared contents to a model provider and retain them in transcripts, logs,
backups or other copies. Closing sharing prevents further broker reads; it
cannot recall already-read data or erase copies. Other sessions at the same
workspace location can use the tools; this is not a per-session boundary.
Treat shared filenames, text and images as untrusted input, including possible
prompt injection. The host command warns even when native Build permissions
would otherwise auto-allow a tool call. Never share credentials or private data
on the assumption that an additional native prompt will appear.

This uses OpenCode's experimental client-MCP WebSocket API. An unsupported
native version refuses sharing without editing configuration. It is not a
promise of compatibility with every V2 version or an accepted live host pass;
see the focused validation requirements in `docs/testing.md`.

### attach

**Experimental, host-only, V2 2.2.6/2.2.7/2.3.0 only:** disclose one host image
to an explicitly selected native session in an already running workspace.
`--session` is required in both modes. The session must belong to the workspace's
exact native project directory; observed archived targets or staged native
reverts are refused. This command never starts a workspace, refreshes auth/config,
unlocks the vault, or selects a session automatically. Put `attach` first.
The native API has no atomic precondition for this check: a revert staged between
the final target check and prompt admission can still be committed by native
OpenCode. Do not stage/revise a revert concurrently with direct attachment.

The ordinary native TUI runs inside the container and cannot directly resolve an
outside host path. This `attach` command is not a transparent interceptor: choose
the broker workflow below, or issue a direct host command. The separate
`--host-drop` TUI launch option has a different host-input authorization model;
it does not weaken this broker's per-file confirmation. Neither workflow modifies
mainline OpenCode or relocates its container plugins.

For repeated screenshots, start a foreground broker once in a second **host
terminal**, using the ID of the intended native conversation:

```sh
oc-box attach --broker --session ses_example --project ~/devel/demo
```

Keep that terminal open. In the OpenCode conversation, drop/paste the screenshot
**path** and explicitly ask it to use the printed MCP server's `request_image`
tool, followed by `read_attachment(id)`. For example, ask: “Use request_image for
/tmp/Spectacle.example/Screenshot.png, then read_attachment with the returned ID
and examine the screenshot.” The host terminal shows each proposed file and its
disclosure warning. Independently type `attach` there to authorize **each file**;
an agent's request, native permission approval, earlier approval or pasted path
is never host consent.

`request_image` captures an approved image and returns only an opaque expiring
ID. `read_attachment(id)` returns that captured image at most once, to the same
native caller session verified through native `_meta` context. It does not reopen
the host path. Wrong-session, missing-context, expired and reused requests refuse.
The broker exposes no `list_directory`, text-read, live-read or clipboard tool.
Unlike `share`, it grants no continuing access to a folder or file. Broker mode
forbids a positional PATH, `--text`, `--send` and `--acknowledge-risk`; it always
requires independent typed host approval.

Alternatively, append one native image **user message** directly from a host
terminal:

```sh
oc-box attach /tmp/Spectacle.example/Screenshot.png \
  --session ses_example --project ~/devel/demo
oc-box attach /tmp/Spectacle.example/Screenshot.png \
  --session ses_example --project ~/devel/demo \
  --text 'Explain this screenshot.' --send
```

Read the warning and type `attach`, or use `--acknowledge-risk` for explicitly
authorized noninteractive direct use. Without `--text`, the message text is
`Please examine this host image.` This is a queued native user message, **not a
draft attachment chip**. Without `--send`, oc-box requests native `resume: false`;
with `--send`, it requests queueing plus `resume: true`. An already active native
execution may consume queued input even without `--send`: neither mode promises
that inference stays stopped. Direct mode requires PATH.

Both modes capture a single regular, single-link PNG/JPEG/GIF/WebP image of at
most 8 MiB using the sharing path/content guards. Folders, symlinks, special
files, protected host/application/credential paths and unsafe source changes are
refused. No project staging file, mount, host listener, saved MCP configuration
or container recreation is introduced. oc-box does not forward the host path or
image bytes in native argv, or record them in its activity journal or project
files. A path you paste into OpenCode or submit as a tool argument is itself
disclosed to native history/provider processing.

The foreground broker holds an explicit existing-workspace client lease. Ctrl-C,
workspace stop, disconnect or native reload ends that connection and further
retrieval; no reconnect or regrant is automatic. Exit the broker when done so
normal owner synchronization/stop can proceed. A new broker needs fresh consent.

**Disclosure persists:** native OpenCode may normalize/store the image and send
it to a model provider; transcripts, logs, backups and agent-created copies can
retain it. Grant expiry, one-use delivery and broker shutdown cannot recall data
already disclosed. Images and pasted paths are untrusted input, not a guarantee
against prompt injection. These experimental workflows have separate validation
requirements in `docs/testing.md`; earlier `share` host acceptance does not accept
this attachment feature.

### sync

Refresh host conversation archives without opening the TUI. Requires an
existing workspace and an intact project directory. It starts or recreates the
container if needed, prepares its runtime/configuration, discovers saved
sessions, exports them, and imports separate namespaced archive copies into the
host database. It stops the container afterward, even if it had been running.

When recreating a missing container for a registered workspace, `sync` can save
and apply `--cpus`, `--memory`, and `--pids-limit` overrides. It refuses explicit
limit changes if an owned container already exists, even when stopped; use
`reset` for that case.

Manual `sync` retains ordinary runtime preparation and native service startup,
including configured integrations and native scheduling behavior. It is not the
isolated, non-executing post-stop reader used by automatic timeout recovery.
Both use the same checked `sync_sessions` archival implementation; automatic
recovery does not recursively launch this command or repeat its preparation.

On V2, discovery reads every native API page at the workspace's internal project
directory, including archived conversations, forks, and linked child/subagent
sessions. Separate conversations are never merged. Sessions at other locations,
including nested directories, are outside this exact-directory scope. Automatic
resume still selects a top-level conversation, not a recently active child.
A successful complete inventory removes missing/deleted or moved-away sources
from active export tracking while retaining their last private and host archives.
An unavailable or invalid inventory is an error, never evidence of deletion.

V2 archives remain independent top-level host conversations. Parent and fork
provenance is retained in versioned `info.metadata["oc-box.archive"]` annotations,
with workspace-namespaced session and fork-boundary message IDs. These annotations
are informational, not authority to restore permissions, select projects, or
delete conversations. Other metadata is preserved; an existing value at this
reserved key is retained under the annotation's `sourceMetadata` field. Native
parent links, fork state, permissions, and revert controls are not reactivated.
Replacing an archive refuses observed native host child links, because native
deletion would recursively remove those conversations. Successful V2 imports are
re-exported to verify transcript and metadata before marking them synchronized.
Treat generated host conversations as archive mirrors, not bidirectional working
copies. Do not edit them or create native children while sync runs: the child
preflight and replacement are not an atomic transaction with unrelated host clients.

An active oc-box launcher holds a workspace lock, so exit it before syncing.
Use this after an OpenCode crash, missed exit-time sync, or host restart.
Sync failure preserves container data; retry after resolving the error.
For a quiet native 2.3.0 source, manual sync can reuse one private, unregistered
source runtime for the pass instead of starting one for every export. Read-only
preflight excludes durable jobs of every status, schedules, queued input and
execution claims; execution-denial configuration and fresh discovery roots
prevent integrations from being used by this optimization. Known pre-start
incompatibilities keep the existing slower per-session path. Unsafe stores,
unexpected startup/activity or unverified cleanup remain failures, not permission
to replay an uncertain pass. The optimized source has a ten-minute active-lease
budget with separate bounded startup/cleanup; verified per-session progress is
retained if a later operation fails.

This optimization does not apply to existing owners or post-stop recovery
readers, and does not change their readiness/stop guarantees. Destination
replacement, child guards and fresh-native verification remain serialized and
unchanged. No metadata timestamp shortcut or parallel database mutation is used.
Overall success is announced only after the private source lease closes cleanly.

The [final history result](#lifecycle-output) distinguishes partial or
unverified work from success. Detailed sync diagnostics use aggregate session,
exported, unchanged, updated and pending counts, elapsed time and a fixed source
mode (`post-stop-reader`, `private-endpoint` or `standalone`), not session IDs,
titles, commands/argv, environment values, native errors or model values. This
feedback does not speed up source exports or archive writes; further optimization
is deferred.

V2 exports are checked against the paginated projected transcript. An unfinished
tail assistant, running user shell/compaction, or changing transcript leaves that
session pending, while other sessions can still synchronize. In an inactive
session, an unfinished assistant followed by settled messages is superseded:
sync mirrors native's export and annotates the omitted count. The exception is
refused while that session is active. See the Sessions and recovery section.
History archives are not full backups of pending input, execution state, or
persistent shell terminals; use encrypted workspace backup for full private state.

```sh
oc-box sync --project ~/devel/demo
oc-box sync --workspace WORKSPACE_ID
```

### web — browser interface

Open Studio around the native OpenCode 2.x browser UI for a workspace. If its
TUI or web owner is running, attach to that private service without waiting for it
to exit or unlocking/refreshing credentials. Otherwise perform a normal V2
startup (including global or selected vault unlock) and own a foreground web lease.
New projects can be created using the host's default distro. Requires host
OpenCode 2.x; 1.x has no web command through this launcher.

```sh
oc-box --project ~/devel/demo
# While it runs, use another host terminal:
oc-box web --project ~/devel/demo
oc-box web --project ~/devel/demo --session SESSION_ID
oc-box terminal --project ~/devel/demo
```

The browser uses upstream's short-lived one-use pairing link and opens an
authenticated common web shell at `/auth/oc-box-sessions`. This first-party
`/auth/` path bypasses native's service-worker navigation fallback without
changing or disabling that worker. Mirrored TUI tabs are eligible on any
canonical stable native `2.x.y` release, including `2.3.2`; prereleases and
noncanonical versions are not eligible. The release number permits a check,
not a guarantee: the native reporting APIs and private saved-tab layout can
change within 2.x, and not every release has been tested.

Studio replaces the compact session index. Studio, Dashboard and Chat focus
change the layout, not the selected native conversation.
The separate no-chat manager distinguishes
verified open tabs from on-demand, read-only history for the registered
workspace project. It makes other views unavailable rather than granting host
catalogs, logs, workspace/backup operations, or a host terminal. It is not
the offline demo's Full TUI or the local `oc-box tui` manager.

The one-use link is the only browser sign-in method and the HTTP relay binds
to `127.0.0.1` only. Vault unlock supplies workspace credentials, not browser
authentication. Named sign-in, LAN binding, and HTTPS/CA setup are deferred.

The index lists all verified open native TUI tabs in this private service,
including idle sessions, in native order in one outer tabstrip. Click a tab to
show that conversation in the single unmodified native app iframe and update
the selected highlight/URL. Refresh TUI tabs updates membership without reloading
the iframe or discarding a loaded draft. A selected conversation closed in the
TUI stays visible as a browser selection outside the TUI-open count.
Native browser tabs and controls inside the iframe remain client-local and
are **not** automatically seeded or synchronized with TUI tabs. Already-open
archived sessions, moved/worktree locations and saved child IDs are not silently
filtered: each listed ID must belong to a fresh native TUI report, or the
verified saved-membership fallback, and pass an identity-matching GET from this
same private service. Unopened history and
missing/deleted IDs are not listed. Native normally groups child navigation
under its root tab; the index does not invent a family tab.
Global TUI tab scope does not combine private workspace services or host history.
For restored workspaces, the saved scope uses the retained internal launch path;
sessions within that scope may legitimately have other directories.
Without a usable fresh report, the shell may use the known private `tabs.json`
layout for saved membership only, never current focus. It checks the pinned
state and verifies each displayed ID through the same private service. An empty
saved scope stays empty. Missing or incompatible state keeps the existing
unavailable/retry behavior; the shell does not search for other state paths,
invent tabs or list historical sessions.
The chooser always offers **Open native OpenCode Home**, including when there
are no TUI tabs, saved membership is missing, or an authenticated index query
fails. This explicit link opens the unmodified native Home at `/`, where you can
create or open a browser session without first running a TUI. It does not choose
historical sessions, add fake TUI rows, or create a session by itself. Browser-only
tabs remain native-client local and do not populate this TUI membership list.
`--session ID` explicitly selects an existing ID in this same private DB, even
when it is not a TUI-open member, and does not add a fake open tab or create a
session. It wins initial selection. Without it, a URL-selected ID is verified;
otherwise a fresh report from the container-native TUI supplies the exact current
conversation and ordered open roots. A routed child stays the exact iframe
conversation while its separately verified root is highlighted in the tabstrip.
Native read-only child views remain read-only: return to an editable root
conversation to compose or add attachments; oc-box does not add a child composer.

Normal owning preparation on an eligible native release installs a small
public-SDK helper inside the container. Reporting requires a compatible
protocol handshake and a fresh snapshot verified against the same private
service. A fresh report from the owning TUI is preferred, even when
blurred; opening a browser naturally blurs its terminal. If it is gone, exactly
one fresh alternate TUI may supply the selection. Multiple alternates, expired,
missing or invalid reports require explicit selection: no first-tab, timestamp,
foreground-activity or old owner-declaration guess is used. Reports are bounded
cooperative UI metadata, not client attestation or host-file authorization.
Selection is verified at snapshot time, not continuously followed.

An already running owner does not acquire the helper on attach. Compatible
saved membership can work immediately without it; choose a displayed tab, use
`--session ID`, or open native Home. A subsequent normal owning startup can
enable reporting. Attach never installs the helper, refreshes
configuration/credentials or unlocks the vault. Project/managed plugin policy
may disable reporting without preventing explicit browsing. See
[the reporting lifetime and limits](ui-state.md).

Standard same-origin iframe URL observation tracks native SPA
navigation only after identity verification; Home/drafts/foreign server views
assert no selected TUI session. Whole-page reloads and explicit conversation
changes can navigate native content; draft persistence across those actions is
the native client's responsibility. See [the common-shell contract](web-shell.md).
This version policy applies only to reporting and saved membership for mirrored
web tabs. Tab-based automatic owner startup/default-session selection keeps its
`2.2.6`/`2.2.7`/`2.3.0` allowlist and eligible-root policy. History sync and
recovery, browser attachments, host sharing and native authentication retain
their separate compatibility and validation rules.

A host relay binds only `127.0.0.1`, forwards
HTTP/streaming/WebSocket bytes through Podman exec to container loopback, and
does not publish a container port, allow LAN access, or join the host OpenCode
service. Links grant access to the workspace service; do not share or log them.
`--no-open` prints the link without launching a browser. `--port` defaults to 0
(choose a free loopback port). Keep the web command open; Ctrl-C closes that
relay. Browser commands use a sanitized host environment without provider/vault
values; the server retains the owning launch's credential snapshot.

Native HTTP replies force `Connection: close` after each final non-upgrade
response, so compliant browsers use a fresh connection when returning to oc-box
routes after pairing or a native page. Response bodies, including streaming
events and downloads, remain streamed unchanged; WebSocket `101` upgrades stay
bidirectional tunnels. The relay is not a general per-request HTTP dispatcher:
pipelining and clients that ignore the close policy are unsupported. Subsequent
requests on such a socket remain native tunnel traffic, not host-route dispatch;
reserved oc-box routes then reach native handling rather than gaining host access.

The owner keeps the lifecycle lock and waits for attached terminal/web/TUI
clients before final history sync and container stop. Closing one attached
client does not stop the others. `oc-box stop` from another host terminal stops
the whole workspace and disconnects all clients. Backup/reset/purge continue to
require the lifecycle lock. Run normal `oc-box start --project PATH` to join a
running web-owned session with the full TUI. Additional clients accept selectors
(and start's `--resume`) only; change model/session/configuration when no owner
is active. The owning foreground command is needed for web access; closing the
browser tab alone does not close its relay or terminate the owner.

For browser-provided screenshot/image/PDF attachments, opt in explicitly:

```sh
oc-box web --project ~/devel/demo --attachments
```

Pairing opens the same common shell described above: **one outer tabstrip and
one unmodified native app iframe**. The optional addon adds visible attachment
controls to that shell; it does not create another wrapper, tabstrip or iframe,
seed native browser stores, or change the common selection/focus rules. Use the
outer tabs or `--session ID` to select an existing private conversation. It also
follows native SPA navigation only after the common shell verifies that same
private ID. During an unverified route, Home, draft or foreign-server view,
attachment forwarding refuses instead of guessing another target.
Native Home and the shell's direct native fallback links remain available;
those separate direct views do not load this addon.
The addon uses only the existing loopback relay and native pairing/authentication.
Native OpenCode **2.2.6 and 2.3.0 only** are
source-checked; other versions refuse the adapter rather than assuming
compatibility. Omit the option for the common shell with no attachment addon.
Its fixed authenticated script is `/auth/oc-box-attachments.js`; the earlier
independent `/auth/oc-box-attachments?session=...` wrapper is retired.

Wait for the native composer to load, then drag the screenshot from Spectacle
into it (or use the shell addon's drop area/file chooser). Actual browser `File`
objects are copied and forwarded as a file-only standard DOM drop. This avoids
native's competing `text/plain` `file:` flavor turning an actual file drop into
a path mention. There is no model tool request, per-image host approval, private
widget lookup or native source/binary patch. **No prompt/inference Send happens
automatically**: check native thumbnails, edit the native prompt and press native
Send yourself. Native may stage attachment bytes into its container temporary
directory before Send, especially when using its file/path fallback. "Draft"
does not mean the bytes remain exclusively in the host browser.
The adapter cannot acknowledge native draft admission through a public API, so
its forwarding notice is not proof of a thumbnail or successful model input.
Do not drop while changing sessions. Every common-shell selection event and
iframe load invalidates an in-progress file read, including explicit
away-and-back changes to the same ID and metadata-refresh events. The addon
also checks the actual same-origin iframe URL, exact selected route, document
and original drop target immediately before forwarding. Pending SPA verification,
changed documents and detached original targets refuse without automatic retry.
This is not an atomic navigation/inactivity guarantee: a native SPA excursion
away and back entirely between public observations may be unobserved. No native
history methods or private widgets are patched to manufacture stronger evidence.
Addon resources use native's existing `/auth/` service-worker navigation exclusion;
native assets and its service worker remain unchanged.

The adapter accepts signature-recognizable **PNG, JPEG, GIF, WebP and PDF** only:
20 MiB per file, eight files and 40 MiB per drop. Signature checks are not a full
document decoder, malware scan or guarantee of provider support. The selected
native model must advertise image/PDF input for inline model delivery; native
otherwise uses its normal container temporary-file/path route. No DOCX support,
document conversion or arbitrary-file ingestion is promised by this adapter.
The direct native link retains upstream's other attachment behaviors.

Spectacle/browser/Wayland/X11/portal combinations can expose a temporary file as
an actual browser file, URL-only text, or another MIME payload. **Only actual
browser files work here**; the addon does not resolve host paths, dereference
`file:`/web URLs or offer a host file server. Its own drop area rejects URL/text-
only input. Non-file drops inside the native iframe retain native defaults
(including workspace path mentions), not host-file access. Native paste of a
clipboard image remains available when the browser exposes a file item; copying
an image's path is not copying image bytes. No clipboard polling is added.

Native browser draft storage, including **IndexedDB**, still stores attachment
data. Sending can retain it in container history and disclose it to the selected
provider; ordinary history synchronization still applies. Removal/detach is not
guaranteed erasure or recall. Pairing/session cookies remain governed by native
authentication; the adapter does not put the controller password into a URL,
HTML, JavaScript, files or browser storage. Standard browser file authorization
or desktop portal UI may still be required by the browser/desktop itself.
Synthetic DOM/API checks are not an interactive Spectacle/browser or live
rootless-Podman acceptance claim.

After the last client exits, V2 final sync waits up to 30 seconds for the same
private server's foreground sessions and unconsumed background jobs to settle.
An idle persistent shell terminal does not block this wait. Timeout or an invalid
activity response refuses live history export. Unless the guarded post-stop path
below completes in full, history remains pending and the command returns nonzero.
Normal cleanup still stops the container, so running work is not promised to
continue. This is bounded checking, not an atomic freeze of every possible
history producer.
With [`--lifecycle-diagnostics`](#launcher-diagnostics), the
`History wait timeout:` diagnostic reports the wait phase (`foreground-query`,
`background-query` or `poll-delay`) and counts from the last complete, validated
activity/job observation. Job counts distinguish
running, completed, error and cancelled entries, with separate allowlisted
shell/subagent/image kind counts; counts above 9999 appear as `9999+`. If no
complete pair was validated, the observation is `unavailable`, not zero. These
are previous observations, not proof of the current state or that a native
request succeeded: a request failure at the deadline also retains the
`activity-timeout` category.
In this diagnostic mode the existing line keeps its format. A separate
`History wait timing:` line is emitted only for `activity-timeout`, not on
normal success:

* `complete-pairs=N` counts all fully validated foreground/background pairs,
  not just quiet or recovery-eligible pairs.
* `foreground-queries=N,total=X.XXXs,max=X.XXXs` and
  `background-queries=N,total=X.XXXs,max=X.XXXs` count and time API attempts,
  including failed and timed-out attempts.
* `last-query=foreground-query` or `background-query` identifies the last API
  attempt. `remaining-at-start=X.XXXs` is the remaining whole live-readiness grace
  when that attempt began, not that query's individual timeout cap.
* `outcome=returned`, `subprocess-timeout` or `failed` describes that attempt.
  `returned` means the transport/API helper returned, not that its inventory
  passed validation.
* `last-complete-age=X.XXXs` is the age of the last fully validated pair.
  Missing last-attempt fields or observation age are `unavailable`; with zero
  query budget, query counts are zero and those fields remain `unavailable`.

Counts are capped at `9999+` and durations at `9999+s`. Query times are wall-clock
measurements around the existing native API helper, including Podman, Python and
native CLI startup and JSON decoding; they are not pure server latency. Late or
partial responses do not manufacture a complete pair. Neither diagnostic emits
session/job identifiers, paths, commands, payloads, native errors, environment
values or credentials. Timing does not track job identity, distinguish live from
durable jobs, or prove that a job is stuck or orphaned. Timing itself adds no
probes or job cleanup and changes neither validation, the 30-second grace,
polling interval, 60-second request cap, recovery guards nor exit policy.

If automatic post-stop recovery is not attempted, diagnostics can explain its
routing guards with fixed categories. For example, `custom-podman-options` means
the container baseline is outside this conservative path; `non-shell-jobs` is the
legacy fixed label for jobs outside that native version's audited snapshot
profile, including image jobs on 2.3.x. `insufficient-shell-observations` or the
2.3.x counterpart `insufficient-job-observations` means fewer than two qualifying
pairs were retained. Stable native 2.3.x uses the guarded retained-subagent
storage profile; unsupported job kinds still refuse automatic recovery. Control-state,
attached-client and
unverified-owner-close refusals are also distinguished. These labels do not waive
any guard or establish that a job completed. Do not clear control markers, remove
job rows or reset a workspace merely to bypass a refusal.

The 30 seconds are oc-box's live-readiness grace, not a native export/import
timeout or a duration limit for the entire synchronization. This grace remains
unchanged. The two paths differ:

* Automatic owner exit waits up to 30 seconds before live export. An eligible
  timeout, or a supported 2.3.x live-sync failure described below, can then use
  one guarded post-stop reader attempt. Checked exports, host imports and native
  verification take additional time.
* Explicit `oc-box sync` has no original owner's live-readiness wait. It uses
  the same checked archival implementation, with ordinary manual-sync
  preparation and cleanup.

A later successful manual sync does not prove that earlier running work was safe
to ignore.

Native OpenCode 2.2.6/2.2.7 can retain a cancelled background-shell job after a
location reload, even with no foreground activity. Such a row still blocks the
wait; it is not safe to ignore terminal-status jobs generally because their
completion notices can restart foreground work. A successful later `oc-box sync`
checks native exports without the original owner's activity wait and does not
prove that its retained job disappeared. The native job-cleanup issue is not
repaired by the diagnostic or by the persisted-history snapshot described below.
Native 2.3.0 drains the reload case, but can retain a running shell after its
producer terminates. It can also retain durable running subagent jobs after the
old producer closes, while a new unregistered process reports no foreground
activity. Foreground activity describes that process, not proof that durable
jobs finished. Retained work still blocks the ordinary live wait.

For supported native owners, an eligible live-sync failure can trigger **one
post-stop persisted-history snapshot** automatically:

* Native **2.2.6/2.2.7** retains the original shell-only profile.
* Stable native **2.3.x** also permits retained subagent jobs, including mixtures
  of shell and subagent jobs. Image jobs remain outside this guarded profile.
  Canonical releases such as `2.3.0`, `2.3.1` and `2.3.2` share this contract;
  prereleases, build suffixes, malformed versions and other minor series do not.
  Series membership selects the metadata checks, never bypasses them or certifies
  untested future native behavior. Incompatible schemas and recovery forms refuse.
  Other native integrations retain their independently documented version limits.

The 2.3.x contract recognizes exactly one known scheduler column layout:
2.3.0's `schedule` table or the `automation` replacement in 2.3.1/2.3.2.
Missing, ambiguous, extended or incompatible column layouts refuse; **any** scheduler
row refuses, including paused/terminal/unknown statuses and committed WAL rows.
Native 2.3.0 cannot read an automation-layout store through this path.
Subagent recovery may include a bounded `outstanding` list of known shell or
subagent metadata. Image, Code Mode, nested/unknown forms and invalid bindings
remain unsupported; these references are not completed, acknowledged or resumed.

The original timeout route requires two consecutive complete validated
foreground/background pairs with no foreground activity and at least one job,
all allowed by that storage profile. Native 2.2.6/2.2.7 keeps this rule: foreground
activity still refuses its automatic snapshot route.

On supported 2.3.x, two consecutive complete validated pairs with nonempty
foreground activity and only supported shell/subagent jobs can instead request
post-stop archival, including when there are no jobs. A later empty foreground
response resets this foreground-interruption hint; malformed responses or
unsupported job kinds invalidate it. Known non-timeout API errors invalidate
the observation hints; an actual query timeout may retain prior qualifying
pairs, but cannot manufacture another pair. **Neither hint permits live export
or establishes that work finished.**

Supported 2.3.x can also route positively classified transport or unsettled-history
failures during live wait/sync through this same one-shot reader when a safe source
was captured. Unclassified failures do not enable a retry. Explicit
compatibility or Reader refusals and failed/uncertain source cleanup are not
retried automatically; unexpected programming/data-shape errors do not enable
this route. It does not broaden legacy or unsupported-version recovery.

After the owner closes normally and all clients detach, oc-box verifies and stops
the same existing, ready container. If stop intent is unchanged, it starts that
exact container once for an isolated history reader, never a replacement or a
normal owning launch. There is no runtime/configuration/auth refresh, vault unlock,
credential forwarding, controller publication or requested model/tool execution.
This uses the normal checked archival implementation through the fenced reader;
it does **not** recursively invoke ordinary `oc-box sync`, prepare the workspace
again or refresh credentials. Native 2.3.x job metadata is checked before source
or destination native history processing: image/unknown jobs, unknown recovery
forms, legacy background state and incompatible schemas refuse rather than
starting managed job recovery. Retained jobs, execution claims and queued input
are not acknowledged, cleared or replayed.
The transition budget is 35 seconds and the snapshot budget 180 seconds, with
separate bounded cleanup. The container is stopped again before exit.
This is **one automatic attempt only**, not a fast reader followed by a second,
unrestricted manual sync. If the reader refuses or cannot finish, oc-box does
not perform ordinary manual preparation, restart native work or replay requests
to try again. Manual `oc-box sync` guidance remains for incomplete or unsafe
automatic recovery, after resolving its refusal.

The guarded post-stop copy reuses one private, pass-local Python export helper
against the Reader's existing endpoint instead of starting a container/Python
command for each session. It still reads the complete inventory and performs
the same serial projected-history/native-export comparisons for every session,
including unchanged ones. Each request is admitted separately against stop
intent and the remaining snapshot deadline; session IDs are not sent as a bulk
work queue. Pending sessions do not prevent verified progress on their neighbors.
The helper handles one response at a time (at most 1 GiB per response), never
starts a native service, and is reaped before overall sync success. Malformed or
failed transport is not automatically restarted or replayed; data and retry
guidance remain. Destination imports and independent native verification remain
unchanged. Ordinary live/manual sync and V1 retain their existing transports.

This conservative path requires a passive container entrypoint, no custom Podman
arguments or automatic health/restart/shell-loader hooks, and validated identity/home mounts.
It also refuses native scheduler rows (including paused/completed rows), unknown
scheduler schemas, unsafe stores and nonempty system OpenCode configuration.
Read-only executable-version probes must match the captured supported release
on both sides; a binary changed during the session does not inherit that proof.
The host and container must match the captured exact patch version, not merely
belong to the same 2.3 series. Unsupported versions are warned about before the
owning client opens; they retain normal live sync and manual-sync retry guidance,
not an unsafe recovery fallback.
The existing runtime checks validate the selected contract on each attempt:
recognizing a future stable 2.3 patch does not waive its database or API checks.
Recognized schema/metadata or activity-response incompatibilities also print an
actionable compatibility warning when detected. Check for an oc-box update
first; upgrade OpenCode too if it is older than a supported release. If both are
current, report the fixed refusal category. Do not reset or downgrade workspace
data to bypass a compatibility refusal. A matching schema alone cannot certify
unknown native startup behavior; unrecognized minor/major series remain refused.
Column-layout checks cover names, types, non-primary-column requiredness,
primary-key membership and generated-column flags, but do not fingerprint
defaults, triggers, indexes or foreign keys. They cannot detect every
schema-preserving behavioral change.
Source and destination helpers use isolated configuration, execution denials and
disabled scheduling/warming; disabling scheduling alone is insufficient because
native restart notification handling can still wake work. Other failures retain
the normal pending-history/manual-sync behavior.

Snapshots retain the existing complete-inventory, checked-export, pending-session
and verified-host-import safeguards. Pending sessions do not prevent checked
progress on other sessions; their prior archives/checkpoints remain intact.
If any remain unsettled, `snapshot-incomplete` reports the partial result with
an interruption warning, never an all-history success. Native startup/projection
can maintain its database; this is not a byte-read-only database backup. No native
job is deleted, rewritten or declared completed, and no queued input is
acknowledged.

**A raw client exit of `0` can return CLI status `0` after an eligible live-sync
failure only when the existing guarded reader completes every checked export,
import and native verification, recovery cleanup succeeds, the final stop is
verified, and the foreground-interruption hint was not used.** Saving history
after a foreground-interruption timeout still returns nonzero, even if all
archives were saved and the raw client exited `0`. Archive-sync success is
reported only after clean stops; it is not work-completion success.
The recovery result still states that execution was interrupted and background
completion is unconfirmed: completion notices or final results may be missing.
This policy is the same for owning native TUI and web launches. Actual raw client
failures, including exit `7` or cancellation, are never converted to success.
Unknown raw client status, pending/partial history, any sync/recovery failure or
failed/uncertain cleanup remains nonzero. Guidance to retry `oc-box sync` remains
when automatic archival cannot safely finish, not as the first response to an
eligible live failure. Resolve the refusal before retrying, without resetting
or purging the workspace.

Historical job rows can remain running-looking on the next owner; the same
bounded post-stop path may save another snapshot, but does not make the underlying
native owner healthy. Neither job age nor a terminal-looking status is a recovery
exemption. Recovery evidence and release-validation requirements are documented in the
[test strategy](testing.md#reviewed-focused-recovery-host-acceptance).

`--no-vault` is accepted only when web becomes a new V2 owner; an attached web
client rejects it and never changes the owner's credential snapshot.

### tui — experimental workspace manager

Open the optional Textual workspace dashboard, separate from the native OpenCode
conversation TUI launched by `start`. It is available on stable `1.8` and
development `1.9`; older stable `1.6` has no dashboard. Check
`oc-box tui --help` for your installed build. It requires optional dashboard dependencies, a regular
unprivileged Linux host user, and real TTY input/output. Normal CLI commands
retain their stdlib-only baseline.

For an existing installation with a launcher release supporting upgrade extras,
request the dependencies through the verified existing owner:

```sh
oc-box upgrade --tui
# Or add both independent extras:
oc-box upgrade --vault --tui
```

This also fixes “Optional TUI dependencies are missing” without rerunning curl.
GitHub installs dependencies privately in an immutable release; verified uv/pipx
owners use their own manager. Omitted extras are not removed, the same release
can gain dependencies, and a repeated satisfied request is a no-op. No workspace
reset, history deletion, credential-policy change, vault initialization/unlock
or dashboard launch occurs. Older launchers must upgrade normally to a supporting
published release first; genuinely pre-channel builds retain the exceptional
[bootstrap route](update-channels.md#bootstrap-and-older-installations).

```sh
oc-box tui
oc-box tui --project ~/devel/demo
oc-box tui --workspace WORKSPACE_ID --read-only
oc-box tui --demo
```

When testing the manager against the **oc-box checkout itself**, run a normal
isolated package, not an editable launcher inside that workspace:

```sh
uv run --no-project --with '.[tui]' python -I -m oc_box tui --project ~/src/oc-box
```

`--with-editable` makes the checkout trusted launcher code. Existing attachment
validation then correctly refuses exposing that launcher through its own project
mount (`trusted-path-exposure`). The isolated package avoids this overlap without
changing the workspace, its owner or the validation guard. `--demo` and isolated
unit tests can still use editable source because they do not attach to that mount.

Initially select one existing project/workspace, or omit selectors for the registered
workspace overview. The header workspace picker permits switching registrations
from every section without connecting to a native source.
Browsing does not create/start a workspace, take lifecycle
ownership, reset, sync, refresh credentials or unlock the vault. `--read-only`
disables native session mutation, backup creation/restore, workspace
removal and terminal control/input. Explicit backup inspection remains available.
`--demo` is a
synthetic-only preview with fixed CPU/RAM/storage readings that also permits
execution inside a container: no host services, actual registry/log access or
host/workspace actions. Refreshing demo data does not animate those sample values;
use normal host mode (optionally `--read-only`) for live measurements.

The seven sections are Status, Settings, Sessions, Terminals, Logs, Storage and
Backup/restore. `1`–`7` select
them; `r` refreshes and `q` quits outside a focused embedded terminal. A left
navigation rail is used on wide terminals; below 118 columns or 32 rows it becomes a header
section picker. The header keeps case-insensitive literal
log search available from every section, including while follow is paused; `Ctrl+L`
opens Logs and focuses search. `s` or `F2` navigates to the Settings tab outside
focused text inputs or the native shell. A focused terminal keeps its keys,
including `s`, `F2` and `Ctrl+L`; use `Ctrl-]` to detach its client without ending
the native shell before using dashboard shortcuts.

Nonempty editable search and form inputs have a double-width red `❌` inside the
field's reserved right padding. Both icon cells are clickable.
Click it to clear that field and retain editing focus, without submitting,
connecting or accessing the clipboard. Password and disabled fields are excluded.
Session/log filtering waits for a 120 ms typing pause rather than rebuilding
tables on every key. Input editing remains immediate. Unchanged views skip
rebuilds, and favorites I/O is asynchronous; native discovery remains explicit
and monitoring keeps the selected refresh interval.

The header count says **N log entries** with no search and **N log matches** with
a search, scoped to the selected Logs source. Clearing the search restores the
unfiltered count; it does not delete retained logs. Hover the count for details.
The count follows the current filter even while selected log output is frozen
for copying.

Drag-select visible text throughout the dashboard, including logs, graphs,
terminal output and control labels. Releasing the mouse copies immediately;
dragging a label never activates its control. Graph bars use copyable `█`/`░`
glyphs. Completed text double/triple-clicks and keyboard selection in text fields
also copy (`Ctrl+A`/`Ctrl+Shift+A` select all field text). Refresh, focus,
programmatic selection and container output never
trigger copying. Forwarded `Ctrl+Shift+C` can repeat a copy;
`Ctrl+Shift+Y` copies the whole visible screen. The same shortcuts work in ordinary
dialogs and successful copies show a confirmation. `F1` explains clipboard
shortcuts and terminal paste behavior. Mouse selections copy the visible
viewport, not offscreen history. The Logs RAW view has a separate **Copy all**
action for matching retained entries, within the same clipboard limit.
**Paste copied** (`Ctrl+Shift+Insert`, shortcut only) uses only the last text copied inside the
dashboard and targets the focused text field or connected shell. For the desktop
clipboard, use the host terminal's paste command, commonly `Ctrl+Shift+V` or
`Shift+Insert`. Ordinary shell `Ctrl+C` and `Ctrl+V` remain unchanged. Clipboard
actions cannot weaken read-only mode or start a connection. The dashboard never
journals clipboard text or creates terminal recordings; copies are
bounded to 1 MiB. Terminal pastes retain the 64 KiB UTF-8/framing/queue limit:
size/capacity refusal queues no prefix; transport errors warn that delivery may
be partial and must be inspected before retrying.
Host copying requires terminal OSC 52 support/permission; container-originated
OSC clipboard requests remain blocked. See `docs/tui.md` for clipboard limits.

Status is a card-based Overview: host CPU, RAM, OS-reported Home FS capacity and
running/registered workspace counts, followed by a workspace inventory and
selected-workspace inspector. Wide host cards collapse to a compact resource
panel at narrow widths. The inspector folds below the table on compact layouts
and remains explicitly expandable. Native source context stays distinct from the
selected workspace. Short CPU/RAM histories use fixed 0–100% scales, not
auto-scaled sparklines. Workspace CPU
numbers retain 100% = one core; the meter uses observed CPU quota when known.
Unknown/unlimited capacity and unavailable readings are not shown as fake zeros.
Raw report data and totals are unchanged. Full details/mounts remain in an
expandable section, initially collapsed. Host readings and monitoring settings
remain available with no current workspace.

The workspace table uses only supplied project/state/CPU/RAM data and explicit
private-home scan results. Stopped/unknown readings are not fabricated zeros.
The retained-activity card shows existing oc-box journal categories only; it does
not load external logs, inspect agent calls or add GPU/token/cost telemetry.
Storage/Backup use scoped cards while retaining all explicit action reviews.

Status and followed logs share an adjustable **whole-number 1–60 second**
refresh interval, default **1 second**, and an auto-refresh control. Cheap metrics
are batched; slow queries never overlap or queue refreshes, so cadence is
best-effort rather than an exact timing guarantee. Expensive allocated storage
scans remain a separate manual action for the selected workspace. Native session
inventories still require manual Connect/refresh.
An animated indicator and source/stage label remain visible while connecting or
loading an inventory. Connect is disabled until the bounded request finishes;
Cancel or a source change visibly cancels its result without automatically
connecting a replacement. Existing rows remain available during refresh.
Their rows, selection and scroll position are not rebuilt by background status,
log or storage refreshes. Use Connect / refresh (or `r` on an already-connected
Sessions tab) to fetch a new snapshot. Explicit filters/project selection and
session actions can update the displayed rows.

All preferences appear directly in the Settings tab, without an extra window
or duplicate monitoring controls. The categorized inline form covers
Monitoring (auto-refresh/interval), Display
(built-in Textual theme/details), Sessions (scope/text filter only, no automatic
connection), Logs (follow/source/severity/search), read-only Resources (saved/observed),
and read-only Safety/terminals (CLI-selected mode). **Reset to defaults** fills
the form with factory dashboard preferences; click Apply to activate them, or
Revert/Escape to discard the reset. Defaults include one-second automatic refresh,
the `oc-box-navy` theme, collapsed details, global session scope, followed logs from all
loaded sources, and empty search filters. It never resets containers, history,
favorites, retained logs or the CLI-selected safety mode. Revert/Escape discards edits;
Apply changes only allowlisted dashboard preferences **for this session**, with
no persistence or logging. Search text is never persisted. These preferences cannot
weaken `--read-only`, modify OpenCode `cli.json` or host settings, or change
workspace resources or credentials. Live CPU/RAM limit edits, storage quotas,
resizes and graphroot/runroot moves are deferred to a separate project. Existing
explicit CLI `reset` remains outside the manager; there is no `podman update`
action, resource-control RPC or resource-editing service extension.

The Monitoring refresh field accepts any whole number from 1 to 60 seconds,
default 1. Invalid or blank entries show inline feedback and apply none of the
pending edits. Reset fills 1; Apply activates it, and Revert discards the draft.
Unapplied monitoring edits do not change the running refresh cadence. Drafts
survive tab switches and background refreshes; resource context updates without
overwriting them. Narrow rounded switches show their ON/OFF state as text and
align with their labels and visible keyboard focus.

The separate **Host logging** card can save the private log directory and the
optional journald mirror without changing session preferences. Demo and
read-only modes cannot save host logging settings. The private file remains
primary, prior data is not moved, and systemd configuration is unchanged. For
rotation, retention and one-off CLI overrides, see
[Dashboard state and activity journal](#dashboard-state-and-activity-journal).

**Logs** offers sortable columns, optional column visibility, row details and
RAW copy of the matching retained snapshot without table formatting. RAW
preserves the safely retained representation, not unvalidated or unredacted input.
An explicit system journal mirror read uses the bounded same-user reader; it
does not enable journald mirroring, merge duplicate private events, or fall back
to a different source. Loading external Podman logs remains separately explicit.

The Sessions tab uses manual Connect/refresh. Host global discovery consumes the
complete paginated native V2 API across projects, children and archived sessions. Host
project views include controller archive associations only where provenance is
verified. Explicit host Connect uses normal native CLI discovery and **may start
the native host service**, even when session actions are read-only; visiting the
tab alone does not. Workspace connections lease only an existing ready private
V2 owner, never the host service. The lease is held until Disconnect or dashboard
exit and visibly keeps owner shutdown waiting. Favorites live in private UI
state, not native pins.

Disconnect is adjacent to Connect / refresh in the source toolbar. Moving the
control does not change loading cancellation, native service ownership or lease
release semantics.

Non-destructive dashboard reviews use the Confirm button alone; they do not
require typing `CONFIRM` or `RESTORE`. Typed operation-specific phrases remain
for destructive actions such as deleting a session/archive or removing workspace
storage. Button-only approval still consumes a single-use review and rechecks
the target identity and permissions before dispatch.

Archive/unarchive uses native V2 archive state. Archived V2 roots remain in history
and synchronization inventories but are not selected by automatic workspace resume;
when no unarchived root remains, the next normal start creates a new conversation.
Native deletion is recursive,
requires a selected verified source/store, descendant review and typed `DELETE`,
with a 50-entry cascade review limit in this initial dashboard,
and refuses unknown activity/ownership, active/unconsumed work, and running
persistent terminals anywhere in that cascade (including idle shells).
Exit those shells before deletion; archiving and history-readiness semantics
are unchanged. Host activity
checks are process-local and warn about races with other native clients; this is
not an atomic cross-process transaction. There is no bulk delete or permanent
mirror suppression. Source and host archive copies are independent, and later
normal sync may replace or recreate a managed host copy. Existing V1 launcher
commands remain supported; these new live manager APIs are V2-only.

Failed or refused deletion shows a reason-specific notification and a persistent
notice. These distinguish active work, running shells, managed copies with native
children, stale reviews, unknown ownership, incomplete inventories and read-only
mode. Transport/protocol failures after submission leave the outcome unverified:
inspect the source before retrying. A completed deletion followed by a failed
inventory refresh is not reported as though the deletion itself was refused.

Storage and Backup/restore offer three **separate scopes**: host OpenCode,
selected workspace, and all registered workspaces. Host actions operate only on
individual native session trees; there is no whole-host database wipe or
replacement. All-workspaces is an inventory for choosing one explicit target,
not an unreviewed bulk-delete authority. Size scans are manual and informational.
Removing workspace storage requires a workspace-specific typed review and uses
the normal `purge` validators/lock. It retains host project files, host history
and verified recovery provenance; it never adds `--all` or `--delete-sessions`.
Exit owning clients first. Purge can briefly start the container for final
history synchronization; no forced stop or lock bypass is offered.

Backup creation, inspection and restore run **inside the dashboard**, using the
same pinned runtime in a separate private child. Enter the archive path; restore
offers an editable, absent destination suggestion. Optional age recipients and
existing identity-file paths are under Advanced. Default password-encrypted
archives already contain an encrypted per-archive identity: no key file needs
copying. The password is not saved and cannot be recovered by oc-box.

Passwords use a custom masked, nonselectable editor feeding the existing child
controlling-TTY reader, not an ordinary Input field. The manager now temporarily
holds secret input in memory; guaranteed erasure, same-user debugger, swap or
recording protection is not promised. Secrets never enter clipboard actions,
widget values/messages, protocol metadata, argv, environment, plaintext files
or journals. Private operations refuse Textual debug, developer tools and
event-file logging. Clipboard/navigation shortcuts are fenced until child exit.
For password confirmation, click its box or Shift+Tab before typing; queued
first-password text is discarded.

The operation modal shows the actual stage and elapsed time. Full and portable
backup creation shows measured encrypted bytes during streaming, checksumming,
and archive packing. Stages without a measurable total show activity, not an
invented percentage or finish time. Cancel or Ctrl+C requests
normal cleanup and waits for real child exit; no automatic retry runs. A backup
can stop an unowned source before requesting its password, so cancellation can
leave that source stopped. Workspace full/portable `.ocbox` archives retain the
normal CLI contracts below: fresh IDs, no overwrite and stopped restores.
Whole-workspace archives may contain copied config/auth. Ordinary CLI password
prompts remain unchanged; Storage removal retains its separate reviewed
foreground handoff.

Native V2 session-tree history instead uses encrypted `.ocsession` files:
versioned, authenticated age components, at most 50 sessions, 100,000 messages
and 64 MiB encoded history. Select the corresponding verified source in Sessions
before backing up or restoring history. No private workspace is started for this
connection. Workspace and session restores show authenticated **metadata only**
for an additional in-dashboard review. Click the reviewed restore action or
focus it and press Space; Enter cannot approve this next review after password
submission. Session restore assigns fresh session/message IDs, clears
permissions/privileged configuration, and restores native parent links only
within that new tree. It never executes restored work or overwrites old IDs.
Partial failures and cancellation retain observed/attempted IDs for explicit recovery; they
are not atomic restores and trigger no automatic rollback/delete or retry.

The dashboard cannot delete backup files: it cannot guarantee that a path
still names the reviewed file at deletion. To remove an archive, verify it in
your host file manager and delete it there. No tab prunes shared images,
arbitrary caches, global vaults, retired provenance or host databases. Session
search text, backup paths and transcript content are not added to the activity
journal.

The Terminals tab embeds the existing native persistent-session terminal client
through a local PTY and optional `pyte` emulator, not a new container shell. Attachment
validates the selected source's membership; a foreign session/host archive is not
authority for a workspace shell. Create/takeover requires explicit confirmation.
Closing the dashboard closes its clients/leases only, not the workspace owner or
native persistent PTY. This is not a complete xterm implementation: no mouse,
images, complex-grapheme or extended-keyboard support is promised. Actual embedded
rendering/input/resize/detach acceptance still needs a new clean-host interactive
run. Immediate attachment refusals include fixed categories such as
`workspace-not-running`, `workspace-identity`, `owner-unavailable`,
`session-unavailable` or `client-launch`. They identify the failed stage without
printing native output, credentials or control replies. Compare the ordinary
`oc-box terminal --project PATH` attach-only path for the same running workspace;
do not reset, repair or start a replacement merely to bypass a refusal.

The Logs tab defaults to `log/activity.jsonl` under the configured state root,
with rotations `.1` through `.3`: four files capped at 256 KiB each, private files
0600/log directory 0700. An existing owned, non-group/world-writable state root
can retain its legacy permissions; it is not automatically chmodded.
Readers do not create files. The dashboard shares validated severity and
message formatting with `oc-box logs`, but displays all retained levels,
including `debug`. Generated entries include only fixed categories and
allowlisted metadata. The ordinary log buffer retains up to 2,000 entries,
with up to 1,000 private-journal entries read per refresh. **Load mirror**
explicitly reads a separate snapshot of up to 1,000 current-user system-journal
entries; the mirror does not participate in ordinary follow or the ordinary
buffer's cap. There is no native OpenCode logfile reader.
Podman logs are a separate explicit opt-in for a selected verified owned
container: tail 500 lines, 1 MiB byte cap, 10-second timeout, memory only. Common
credential redaction is best-effort, not proof that arbitrary secrets are absent.
Source filters/follow controls do not disable search. See `docs/tui.md` for
design, installation and safety boundaries. Demo/headless/native offline checks
are not host acceptance.

### terminal

On V2, the default connects to the active session's native persistent shell
terminal, also shown in OpenCode's TUI/web terminal panel. It can run concurrently
with those clients. The currently controlled running PTY is reused (otherwise
the last running PTY), or a new session terminal is created when none exists.
It uses the session project and the running server's environment. `Ctrl-]`
detaches without ending the shell; reattach to retain shell variables and state.
Typing `exit` ends the shell. Connection takes interactive control and propagates
terminal dimensions; TUI/web clients can regain control in their terminal panel.
It does not forward new host credentials, refresh config/auth, or take the
exclusive lifecycle lock. A ready active V2 owner is required; start `oc-box`
or `oc-box web` first. Exit the attached client to release its lease without
stopping other clients. Use `--maintenance` for the shell described below.
On V1 the existing maintenance-shell default is retained.

Open an interactive maintenance shell in an existing workspace container
instead of starting OpenCode. Select the current project, `--project PATH`, or
`--workspace ID`. The [logging options](#launcher-diagnostics) also apply to
`terminal`. Run from a real host terminal with TTY input/output; piped or
redirected input/output is refused.

```sh
oc-box terminal --maintenance
oc-box terminal --maintenance --project ~/devel/demo
oc-box terminal --maintenance --workspace WORKSPACE_ID
```

Exit the active OpenCode launcher first: maintenance holds the same exclusive
workspace lock as other lifecycle operations. This prevents a concurrent reset,
purge, or normal launch. It verifies the original project identity, matching
marker, container ownership, and project mount. It never creates a new workspace
or container, reinstalls tools, refreshes configuration/auth, calls OpenCode,
or synchronizes history. If the container is missing, use a normal launch to
recreate it explicitly. A partially initialized but existing container can be
entered for debugging without rerunning baseline downloads.

If the container is stopped, terminal starts it and stops it again on shell
exit (including error/interrupt cleanup). If already running without an active
launcher, it is left running. A paused container must be unpaused by the host
operator first. Type `exit` or press Ctrl-D to leave. Ctrl-C interrupts shell
commands; terminal sizing, input, and resize events are relayed normally.
The command returns the maintenance shell/Podman exit code. The session IDs,
source files, and private home are not changed by the launcher; your own shell
commands can change them. Packages installed in the OS layer survive stop/start
but not reset, just as when the AI installs them.

The shell runs with the container's existing user, devices, resource limits,
mounts, and privileges; terminal adds no mounts or capabilities. Container root
is still rootless on the host. It opens Bash with `--noprofile --norc -i`, or
`/bin/sh -i` when Bash is unavailable. Shell startup files are not loaded, and
history persistence is disabled (`HISTFILE=/dev/null`, history sizes zero).

Unlike a normal OpenCode launch, terminal does not forward API keys, tokens,
custom `pass_env`, proxy variables, agent sockets, or shell/loader hooks. The
Podman client receives only allowlisted host runtime/storage discovery settings;
its child shell starts with `env -i` and a small fixed environment: the private
HOME, project PWD, container PATH, validated TERM, Bash SHELL, `C.UTF-8` locale,
container marker, and history controls. Credentials are not put in arguments,
printed, or copied into the container by this command.

This is maintenance access to the existing environment, not a second security
boundary inside it. Previously copied auth/config files, secrets deliberately
written to disk, and environments of other processes already in the container
remain accessible under normal container permissions. To inspect environment
variables, avoid posting unredacted output from unrelated running processes.
Starting an existing container also resumes its configured entrypoint unchanged;
terminal does not change create-time environment or undo earlier exposure.

For example, after mapping an NPU in an earlier reset:

```sh
oc-box terminal --maintenance --project ~/devel/npu
ls -l /dev/accel/accel0
prlimit --pid $$ --memlock
xrt-smi examine
exit
```

### reset

Recreate the environment's container OS, then launch OpenCode and resume work.
Exit the active workspace launcher first. Reset removes the owned container
and owned networks while retaining the private home/session volume and any
other workspace-owned volumes, project,
Git history, unfinished edits, session IDs, and resume metadata. Installed system
packages and files outside the project/private home must be recreated; running
processes stop. Baseline tools and OpenCode are reinstalled automatically.
Normal V2 reset resolves the current global vault or explicit credential policy
before deleting the old container. Wrong passphrase, corrupt configured vault,
missing crypto support, or missing selected credentials abort before deletion.
`--no-vault` opts out of vault delivery for this V2 reset only, not future starts.

Use reset to change distro, CPU/RAM/process limits, devices, or extra Podman
settings. These are not live updates in oc-box, even though Podman itself can
update some limits. No purge is required. Native binaries cached in the home
may need reinstalling when switching distributions, especially to/from Alpine.

```sh
oc-box reset --project ~/devel/demo --cpus 8 --memory 16g
oc-box reset --project ~/devel/demo --distro ubuntu
oc-box reset --project ~/devel/demo --device /dev/accel/accel0
```

### repair

Explicitly accept the current directory identity for an existing workspace at
its original canonical path. Use this after a filesystem identity change or a
deliberate project restore when you want to retain the existing environment.
Only run it after checking that the directory is the project you intend to use.
The host registry must still exist. A matching regular `.oc-box-id` is retained;
if the marker is missing, repair can restore it from that registry. Symlink,
invalid, or another workspace's markers are never overwritten. Repair cannot
relocate a workspace or reconstruct missing registry/session data.

Ordinary device-number reassignment after reboot does not need repair
once a stable filesystem/handle baseline has been recorded. Old registrations
that already mismatch and lack that baseline still need one explicit repair;
matching legacy registrations are upgraded by the next normal launch.

Exit the active launcher and stop its container first. Repair takes the workspace
lock, checks container ownership, and refuses a running container. It updates
the registry's directory device/inode and supported filesystem-handle baseline,
and restores a missing marker; project
files, container OS,
private home, sessions, and saved settings are retained. It does not start or
reset the container. An already matching identity needs no change.

```sh
oc-box repair --project ~/devel/demo
oc-box --project ~/devel/demo --resume
```

Accepts only `--project` or `--workspace` selectors, besides help. With neither,
use the current directory. If a missing marker has exactly one registered
workspace for this path, `--project` selects it. If several records reference
the path, repair refuses to choose: inspect `--status` and supply `--workspace
ID` explicitly. With `--workspace`, check the registered path and restore its
missing marker rather than using the current directory.

### purge

Remove this workspace's labeled containers, owned volumes, owned networks,
resume metadata, private recovery exports, and matching project marker.
Exit its active launcher first. Shared base-image caches and unrelated Podman
resources are retained; cleanup does not use a global prune.

Default purge retains project files and host archive conversations. It attempts
a final history sync while the project and container exist. If that sync fails,
the private volume is retained for recovery. If the project was already deleted,
there is no final sync; the last host archives remain.

Before removing the active registry, plain purge also retains its verified
archive JSON files and minimal workspace/project provenance privately under
`~/.local/state/oc-box/retired/ID/` (or the configured state root). These are not
active workspaces and do not appear in list/status. They allow a host archive
that still names the old controller directory to be imported later. They may
contain private conversation content, just like host archives. The explicit
`--delete-sessions` and `--all` variants do not create this recovery copy.

`--delete-sessions` additionally deletes the recorded host archive copies, not
an original host conversation imported into oc-box. `--all` additionally deletes
the entire project directory. Project deletion requires the original directory
identity and matching marker; if either belongs to another workspace, `--all`
is rejected before deleting any history or resources. Extra externally supplied
mounts/volumes are not owned resources unless they bear this workspace's
ownership label.

By default, enter the first eight characters of the workspace ID to confirm.
`-y` or `--yes` skips this confirmation. These deletion flags apply only to purge.

```sh
oc-box purge --project ~/devel/demo
oc-box purge --project ~/devel/demo --delete-sessions
oc-box purge --project ~/devel/disposable --all
oc-box purge --workspace WORKSPACE_ID --all --yes
```

### list

List all registered workspaces with aligned headers: WORKSPACE ID, DISTRO,
PROJECT, STATUS, and SESSION ID. No Podman invocation or container start is
needed. Missing project directories remain listed so they can be purged by ID.
STATUS describes directory identity/marker health, not container running state:
`present`, `identity changed`, `marker missing`, `marker mismatch`,
`invalid marker`, `missing directory`, `replaced directory` (not a regular
directory), or `inaccessible`. Device-only changes with a matching recorded
filesystem ID, persistent handle, and inode remain `present`; status does not
rewrite the registry. Unverified identity changes are not assumed to mean
replacement: legacy records may lack a stable baseline. A marker mismatch
means another workspace is selected for that path. Recovery/cleanup hints go to
stderr and never delete or repair records automatically.

SESSION ID is the original container session used for resume, or `none`.
Local metadata is refreshed at startup/sync; a new conversation in a running
client may not be reflected until the next sync or launch. With no workspaces,
the headers are still printed. The list is global, not filtered by selectors;
use `--status` for a selected environment and machine-readable output.

```sh
oc-box list
```

### distros

List supported distro names, image references, and the detected host default.
No Podman invocation is required. This is a fixed supported image catalog;
there is no custom-image option. Selection is stored per workspace.

```sh
oc-box distros
oc-box --project ~/devel/demo --distro fedora
```

### status

Alias of `--status`. Report all registered environments unless `--project` or
`--workspace` selects one. A selector must refer to an existing workspace.
Status is read-only and can run while OpenCode is active; it does not start,
stop, or execute commands inside managed containers. Use `--json` for numeric,
machine-readable output, or `--table` for a bounded field/value view that omits
saved Podman option values. Status output can contain private paths and
metadata; the default and JSON reports also retain raw saved Podman options.
Review the output before sharing it. The default remains the resource summary.
See Resource reports for metric interpretation.

```sh
oc-box status
oc-box --status --project ~/devel/demo
oc-box --status --json
oc-box status --project ~/devel/demo --table
```

### logs

Read the [oc-box activity journal](#dashboard-state-and-activity-journal)
on the host. The default private-file reader uses only the Python standard
library; no systemd, dashboard extras, Podman, native OpenCode, vault unlock or
running workspace is needed. The command does not create, trim or rotate logs,
start a container, or modify the workspace registry, project files or session
history. It does not read Podman output or native OpenCode logfiles.

Use `oc-box logs --tail` for the preparation view: container creation and
image pulls, baseline packages, runtime tools, the matching OpenCode CLI, and
plugin dependencies. It shows task starts and results, with elapsed time and
exit status on failure. These are written while preparation runs, so you can
watch them with `--tail --follow`. The view defaults to all preparation levels;
explicit severity, workspace, limit, and output-format options still apply.
Raw installer output is not saved: package managers, registries and plugin
scripts can emit sensitive data. Preparation records are stored separately
in `preparation/activity.jsonl` under the selected private log directory,
with the same retention settings applied to that ring independently. This
keeps the ordinary activity log compatible with older launchers. Preparation
records are not mirrored to the system journal. `--tail` cannot be combined
with `--journald` or `--path`.

By default, show the latest 100 retained entries at `info` or above.
`--level debug` includes lower-severity stage detail; `--level warn` or
`--level error` narrows the output. Alternatively use `--verbose=LEVEL`. Bare
`--verbose` or `--verbose=ALL` displays all validated retained levels, up to
10,000 matching entries unless you supply `--limit`. It never reveals unlogged
data or reads a different source. `--project PATH` and `--workspace ID` are
mutually exclusive filters and require a valid workspace registration. With
neither, show entries across workspaces, including entries with no workspace.
Severity and workspace filters apply before the limit. Use `--follow` to keep
watching after the initial output, and Ctrl-C to stop. Use `--json` for one
validated event per line (JSON Lines), including when following. With no
matches, JSON Lines output is empty, not `[]` or a placeholder. The default
reader treats missing or unsafe private journal data as empty.
Use `--table` for timestamp, severity and activity columns; when following,
the heading appears once on the first match. No matches still produce no output.
Lifecycle detail can appear after the invocation finishes: history and shutdown
diagnostics are queued so journal writes do not run inside verification deadlines
or while a client lease is held.

Use `--log-dir PATH` to read a different private journal for this invocation.
It works with the private event filters or with `--path` to print the resulting
pathname without opening the journal. It does not create the directory or
change the saved policy.

Use `--journald` to explicitly read this user's mirrored oc-box activity events
through local `journalctl` with `--output-fields` support (systemd 236+).
The same workspace selectors, severity thresholds,
limit, follow and JSON Lines settings still apply; the system journal reader
uses native journal priorities for severity. An unavailable reader reports
fixed guidance to run `oc-box logs` without `--journald`; it never falls back
automatically. Do not combine `--journald` with `--log-dir` or `--path`. The
writer opt-in and system retention limits are described
under [Dashboard state and activity journal](#dashboard-state-and-activity-journal).

```sh
oc-box logs
oc-box logs --verbose=ALL
oc-box logs --tail
oc-box logs --tail --follow
oc-box logs --path
oc-box logs --log-dir ~/.local/state/oc-box-diagnostics --path
oc-box logs --project ~/devel/demo --level debug --limit 50
oc-box logs --workspace WORKSPACE_ID --level warn --follow --json
oc-box logs --journald --level warn --follow --json
oc-box logs --table --level warn --follow
```

### upgrade

Update the installed launcher/modules and prebuilt manuals from a verified
published release. Run on the host as your regular user; no sudo or GitHub
token is needed. No Podman or registered workspace is required. This does not
update host OpenCode, modify a source checkout, reset a container or migrate
project/session data.

Release channels are independent of installation method: **stable** comes from
`master` as a final release; **devel** comes from `devel` as a GitHub prerelease
and Python `.dev0` package. Both GitHub/curl and PyPI are supported. Upgrades
select immutable published releases, never moving branch HEAD.

Without a saved choice, the channel is stable. A successful explicit `--devel`
or `--stable` selection is remembered by the verified installation owner.
GitHub installations and individual package-manager installations have separate
preferences. A preview or failed upgrade does not save a new preference.

```sh
oc-box upgrade --check
oc-box upgrade --devel --check
oc-box upgrade --devel
oc-box upgrade --devel --vault  # Add vault dependencies independently
oc-box upgrade --devel --tui    # Add dashboard dependencies independently
oc-box upgrade --devel --vault --tui
oc-box upgrade                  # Follow the saved channel
oc-box upgrade --stable
oc-box upgrade --auto-update on
oc-box upgrade --auto-update off
```

GitHub upgrades verify tag/commit identity and selected branch membership before
running the staged immutable installer. Python-package upgrades delegate only
to a verified owning pipx, uv tool or dedicated virtual-environment manager.
An ambiguous owner or editable checkout receives manual guidance rather than
a guessed installation target. Verified uv/pipx owners can receive additive
package-extra requests. Plain pip cannot prove prior requested extras, so
ambiguous extras still refuse rather than guessing what to preserve.

`--vault` and `--tui` independently request optional installer dependencies.
They work from stable or development installations and can be combined with
either channel selection or `--check`. With no selector, the saved channel is
used: `oc-box upgrade --vault` adds vault support without choosing development.
Channel selection alone does not request new extras, and optional defaults are
unchanged. These flags do not change workspace credential policy, initialize or
unlock a vault, launch the dashboard or start a workspace.

Requests are additive: omitted extras are not removed. Only extras supported
by the target are installed, but inherited requests remain saved for later
supporting releases. Older stable 1.6 has no dashboard; stable 1.8 and devel 1.9
support it. Explicitly requesting an unavailable extra, such
as `--stable --tui` against that target, refuses rather than ignoring it.

The same selected release can gain extras even when no newer release is
available. Repeating a satisfied request is a no-op. `--check` validates requested
support without installing dependencies or persisting channel/extra preferences.
The installed launcher must support these flags, and a supporting target must
be published; a branch merge alone supplies neither an installed parser nor a
published release. Older channel-aware launchers should upgrade normally first.

Normal updates do not silently downgrade. An explicit channel switch may select
an older release and warns first. **Replacing launcher files does not roll back
workspace/session state.** Stop active clients, keep encrypted backups and check
compatibility before returning from development to stable.

Exact v1.6.60 needs one plain `oc-box upgrade` before channel-selection flags.
Genuinely pre-channel 1.7 GitHub installations need the channel-aware bootstrap
with an explicit channel; their old ahead-of-master check can retain the
development build.
Older package installations can first update with their owning manager. See
[update channels](update-channels.md) for the bootstrap and package examples.

The installer stages a complete release under `~/.local/share/oc-box/releases/`,
including license and manuals, before switching the launcher. New processes use
the new version; active launchers keep their original release path and lazy-load
its original modules. Old release directories are retained and have no automatic
garbage collection. `current` and compatible top-level installed paths point to
the activated release. An older flat installation is migrated; its old manuals
are preserved as `legacy-docs`. No workspaces or credentials are migrated/deleted.

GitHub automatic updates are off by default and follow the saved release channel.
`--auto-update on` saves `auto_update: true` in the existing host oc-box
configuration, retaining settings such as `pass_env`.
Normal installed `start` launches check before Podman/workspace setup and, when
updated, continue with the same arguments. No background daemon, active-session
update, or automatic check for list/status/terminal/reset/purge is introduced.
Direct source-checkout launches skip automatic updates. A concurrent check does
not block another normal launcher. Network/download failures are reported and
the normal launch continues with the installed version; explicit upgrade failures
return nonzero. Package automatic updates remain disabled:
`--auto-update on` refuses package ownership, while `off` prints package-manager
guidance without changing the GitHub installation's setting. Automatic-update
settings cannot be combined with channel selectors, `--check`, `--vault` or `--tui`.

Fetch/checkout/install subprocesses have a clean, positive-allowlisted
environment. Global/system Git config, helpers, hooks, external protocols,
provider tokens, SSH agents, and user proxy variables are not used by the public
download. HTTPS is required; shell commands or a custom upstream URL are not
accepted. Use manual source installation for a reviewed alternate deployment.
Updates inside a container are refused because its private HOME is not the host
installation. Git is required for public updates; build tools are not.

### secrets

Manage an optional encrypted credential vault on the host. This command never
starts Podman or copies secrets into the development environment. V1's legacy
env-file default is unchanged. V2 workspaces without an explicit credential
policy use this global vault first: all current entries are delivered on each
normal owning start/reset/import/web, including the first project launch.
Use the literal `secrets` command first, followed by one action and, for set or
remove, an uppercase environment name. Values and passphrases are accepted only
through masked controlling-terminal prompts, never argv, stdin, or environment
password settings. Host-only; do not run it inside a development container.

```sh
oc-box secrets init
oc-box secrets set PERSONAL_GITHUB_TOKEN
oc-box secrets set BRAVE_API_KEY
oc-box secrets list
oc-box secrets remove OLD_TOKEN
oc-box secrets passwd
# Override the saved minimum for a new/changed passphrase:
oc-box secrets passwd --min-passphrase-length 8
```

Actions: `init` creates an empty vault after entering/confirming a passphrase;
it never overwrites an existing vault. `set NAME` unlocks, then enters/confirms
the secret value and creates or replaces that entry. `list` unlocks and prints
only sorted names, never values. `remove NAME` unlocks and deletes the named
entry; a missing entry causes a clear error without changes. `passwd` unlocks
with the old passphrase and asks for a new confirmed passphrase. No `get`,
plaintext dump, insecure batch password option, or automated env-file import
is provided. `oc-box secrets --help` describes the action syntax.

Input feedback shows a mask and character count for typing and pasting. Secret
value and confirmation prompts start masked and offer **Ctrl+R** to toggle
visible/masked input on the fly. Passphrase creation, confirmation, and unlock
prompts always stay masked: Ctrl+R does not reveal or modify passphrases. Reveal
state never carries to the next prompt. There is no flag or setting that makes
credentials visible by default.

Editing keys: Left/Right move, Home/End (or Ctrl+A/Ctrl+E) select the ends,
Backspace/Delete remove a character, Ctrl+U clears the entire value, Ctrl+W
removes the preceding word, and Ctrl+K clears from the cursor to the end. Enter
accepts; Ctrl+C, Ctrl+D, or Ctrl+Z cancels without changing the vault. Ctrl+Z
cancels rather than suspending a process that is holding sensitive input. The
normal terminal settings and bracketed-paste mode are restored on exit/errors.

Long values use a width-bounded horizontal viewport with a count, so they do not
normally wrap; only the current field is displayed, never prior vault values.
UTF-8 input is preserved exactly, with byte limits applied before insertion.
Terminal control/bidi/combining characters are never executed/rendered as active
terminal sequences when revealed. Bracketed paste treats its contents as data,
not toggle/edit/submit keystrokes: multiline, control-character, invalid UTF-8,
or oversized bracketed pastes are rejected as a whole. Correct/reenter rejected
input before submission (Ctrl+U clears it); it is not silently truncated. A
terminal without bracketed-paste support cannot distinguish a pasted newline
from Enter, so paste only a single-line token with no trailing newline.

The field is erased on completion/cancellation, including when revealed. This
does not undo exposure to terminal recordings, screen capture/sharing, or
scrollback/reflow. A visible value is an intentional display of a secret on your
host terminal, not a way to export or log it; mask/count feedback also discloses
length. No prompt input is written to stdout/stderr, shell history, registry,
or plaintext temporary files, and the encrypted vault format is unchanged.

Optional cryptography support is required only when using vault operations:

```sh
# Existing installation, with a launcher supporting upgrade extras:
oc-box upgrade --vault
# New isolated Python-tool installation:
pipx install 'oc-box[vault]'
# Or: uv tool install 'oc-box[vault]'
# In a package installation's activated virtual environment:
python -m pip install 'oc-box[vault]'
```

For an existing installation, the upgrade request follows the saved channel and
verified owner, adds dependencies only and does not initialize/unlock the vault
or change credential policy. Use `--devel --vault` when development is wanted.
Older channel-aware launchers should upgrade normally to a supporting published
release first; ambiguous plain-pip extras require manual owner guidance.

For a fresh V2 installation, choose the `oc-box[vault]` extra (or the installer
option below), then run `oc-box secrets init` and `oc-box secrets set NAME`
**before your first** `oc-box --project ~/devel/demo`. Global use needs no per-project
registration or selection. Values are entered only in masked prompts; do not
put secret values in OpenCode or oc-box JSON. Configuration can reference their
names using the published V2 shapes under Configuration and environment.

For GitHub/source installations, `python3 install.py --with-vault` stages the
optional crypto wheels privately in that release (using a temporary venv), not
in system/user-site Python. The curl bootstrap accepts `sh -s -- --with-vault`
when piped to a shell. Future GitHub upgrades preserve that opt-in. The source
can also use a host Python with a distro-provided cryptography package. A
missing dependency yields guidance, not a fallback to weak/custom encryption.
No desktop keyring, external password manager, cloud service, or daemon is used.
With no host vault, normal default V2 launches retain the env-file fallback and
suggest setup; they never install crypto, initialize
a vault, edit config, or import JSON secrets automatically. An initialized empty
vault still unlocks but supplies no vault entries; guidance suggests
`oc-box secrets set NAME`. A present corrupt vault, wrong passphrase, or missing
crypto support aborts before container setup/reset deletion, with no silent
fallback. Native OpenCode saved/OAuth credential refresh and copying remain
independent in every credential mode, including global vault mode: env entries
do not replace structured OAuth refresh-token records. A vault containing only
`BRAVE_API_KEY` does not disable existing Copilot OAuth. Configured global mode
suppresses legacy ambient token/env overrides, including `OPENCODE_AUTH_CONTENT`,
but keeps ordinary supported native auth integration. No plaintext OpenCode JSON
secrets are imported or rewritten into the vault.

AES-256-GCM authenticates the ciphertext and format/KDF metadata; Scrypt uses a
random 16-byte salt with n=131072, r=8, p=1 (approximately 128 MiB memory) and a
32-byte key. Each write generates a fresh salt and 12-byte nonce. Crypto is
implemented by the maintained cryptography library, not handwritten primitives.
Format version 1 accepts only those bounded parameters; tampered costs cannot
trigger arbitrary memory allocation. New/changed passphrases default to a minimum
of 8 characters, configurable with `vault_min_passphrase_length` in the host
oc-box config or `--min-passphrase-length` on `init`/`passwd`. Integers from 8 to
1024 are accepted; longer passwords are allowed up to 1024 UTF-8 bytes. Existing
passphrases unlock regardless of the current creation minimum. Entries are limited
to 128, each nonempty UTF-8 value at most 64 KiB with no NUL, and the complete
encrypted file to 2 MiB. Names, unlike workspace selections, are encrypted too.

The host vault normally resides at
`~/.local/share/oc-box/credentials/vault.json`, or its XDG_DATA_HOME equivalent.
Its directory must be owned by your user with 0700 and the regular vault file
with private 0600 permissions. Symlink paths, extra hard links, wrong ownership,
insecure permissions, invalid formats, and nonregular vault/lock files are
rejected. Files are locked during read-modify-write. Ciphertext is written to a
0600 temporary file, fsynced, and atomically replaced; no plaintext temp file
or key file is created. Do not locate the vault inside a mounted project.

There is no passphrase recovery or permanent unlock/cache agent. Forgotten
passphrases make the encrypted data unrecoverable. Back up ciphertext and store
the passphrase separately. Removing an entry or changing a passphrase does not
erase older backups/snapshots or revoke the provider token. WSL requires a host
terminal and filesystem permissions such as its Linux home filesystem; this is
not a promise that an arbitrary Windows/shared mount enforces Unix permissions.
Python memory is not guaranteed zeroized; core dumps are disabled on secret
prompt entry where permitted, but host debugging/swap/hardware compromise and
secrets delivered to container processes are outside at-rest encryption.
During vault-enabled launches and explicit private captures (including V2 native
saved/OAuth credential transfer with `--no-vault`), host captured subprocess
output uses anonymous Linux memfd storage rather than disk-backed temporary files.
Launcher-generated
failure messages redact exact active vault values. This is not a general
redaction filter for OpenCode, provider logs, tool output, or session archives:
an agent can still print, save, or transmit a credential it has received.

### credentials

View or change **names-only** forwarding policy for an existing workspace.
Accepted modes are `env`, `selected`, and `vault`. Without an explicit saved
policy, V1 defaults to legacy `env`; V2 defaults to global vault-first, including
existing workspaces that have no policy. Existing explicit env/selected policies
remain intentional overrides; an upgrade does not replace them.
With no selection options, print mode and selected names without unlocking the
vault, revealing values, starting a container, or refreshing config/auth.

```sh
oc-box credentials --project ~/devel/demo
oc-box credentials --workspace WORKSPACE_ID --credential-mode selected \
  --env-secret GITHUB_TOKEN --vault-secret PERSONAL_GITHUB_TOKEN
```

Requires the registered project/marker and exclusive workspace lock. Exit its
launcher before configuration; changes take effect on the next launch without
reset or session import. Selectors default to the current directory; no new
workspace is created here. Start a new environment normally once to register
it, exit, then configure **overrides** if needed. Default global vault use already
works on that first V2 launch without this command. Use only selectors and
credential options; session, device, resource, deletion, or update options are
refused.

In `vault` mode on V2, all current global encrypted entries are selected
dynamically each time a new owner starts. No automatic list of names is frozen
or saved in the registry. Adding/removing entries affects the next new owner,
not a running service or attached clients. When a host vault is present, global
mode replaces ambient automatic `_TOKEN`/`_API_KEY` and custom `pass_env`
forwarding; fixed runtime and noncredential routing preferences remain.
Global delivery is limited to 1 MiB of combined UTF-8 environment names/values
including separators. Larger valid vaults remain stored unchanged, but a global
launch fails before setup/reset deletion; use selected mode with fewer entries.
Nonempty per-name env/vault selections are refused in this mode. To switch an
existing selection to global use, clear it explicitly:

```sh
oc-box credentials --project ~/devel/demo --credential-mode vault --clear-credentials
oc-box --project ~/devel/demo
```

In `selected` mode, only the explicitly selected env/vault names are delivered,
plus the launcher's fixed runtime settings and noncredential language/color/
model-routing preferences. This replaces automatic suffix forwarding and global
`pass_env`, not just supplements them. Vault and env names cannot overlap.
Uppercase credential names cannot override loader/shell/host runtime settings
such as PATH, BASH_ENV, LD_PRELOAD, OPENCODE_AUTH_CONTENT, or OC_BOX variables.
Names append/deduplicate; `--clear-credentials` clears both lists before adding.

```sh
oc-box credentials --project ~/devel/demo --credential-mode selected \
  --clear-credentials --vault-secret PERSONAL_GITHUB_TOKEN --vault-secret BRAVE_API_KEY
oc-box --project ~/devel/demo
oc-box credentials --project ~/devel/demo --credential-mode env --clear-credentials
```

Explicit `env` mode retains legacy automatic suffix/pass_env forwarding. It may
also supplement inherited variables with selected vault names; a vault value
then wins over an inherited same-named env value. Saved `vault` mode with runtime
V1 uses legacy env forwarding without unlocking all-global entries; explicit
old env/selected vault selections continue to work as before.

A normal owning start/reset/import/web unlocks once when using a present vault
in global mode or when selected vault names exist. The decrypted values stay
only in the launcher's memory/context and private container process environments. They
are not written to the workspace metadata, host os.environ, command arguments,
config files, or a cache. Values reach the container through Podman's inherited
env names; the vault and passphrase are never mounted. Wrong passphrase, corrupt
configured vault, missing crypto support, or missing selection aborts before
container setup/reset deletion. No vault present permits the default/global V2
env-file fallback; an initialized empty vault
unlocks and delivers no vault entries, with `secrets set NAME` guidance.

V2-only `--no-vault` on owning start/reset/web skips vault entries once without
saving policy: global mode uses legacy env forwarding for that invocation;
explicit env/selected modes retain their env constraints. It is rejected on
attached clients and all unrelated commands. Attach uses the already-running
service and never unlocks or refreshes credentials.

Informational commands, maintenance terminal, explicit history sync, stop,
repair, and upgrade never unlock. The maintenance shell's clean environment
rules remain unchanged. Configuration/auth copying is an independent feature:
these selections do not remove embedded credentials in copied config, provider
or MCP auth files, project files, raw Podman create-time Env settings, previous
processes, or previously saved container data. Selection is reduced forwarding,
not an isolation boundary from secrets already inside the container. Delivered
keys remain readable by the agent/tools with normal container permissions.

### backup

Create one encrypted `.ocbox` file for an existing workspace (available since
1.0.0). Requires host age/age-keygen, Python, rootless Podman, and Linux memfd
support; it does not require the vault
crypto extra or unlock the credential vault. The command must be first, with
its own options after it. Use --project/--workspace or the current directory.

```sh
oc-box backup --project ~/devel/demo --output /backup/demo.ocbox
oc-box backup --workspace WORKSPACE_ID --portable --output /backup/demo-portable.ocbox
```

The workspace lock requires its active OpenCode launcher or maintenance shell
to exit. The command verifies marker/physical identity and resource ownership,
then stops the container if needed; it remains stopped afterward. Host editors,
builds, and other processes must not change project files during capture. The
launcher lock cannot quiesce unrelated host writers. Project and container
configuration must be intact; missing containers and unknown extra bind mounts/
volumes are refused rather than silently omit outside data.

Full backup (default) captures the complete project including `.git`, untracked
files and unfinished edits; the private home with the live OpenCode SQLite DB,
subagent transcripts, snapshots, copied config/auth, and caches; registry/resume
evidence; and a committed OCI image of the modified container OS. Mounted project
and home are backed up separately, not folded into the image. Portable mode
omits that OS image; system packages/SDKs must be recreated after restore.
Neither mode captures running processes, kernel state, devices, host drivers,
the host global database, or arbitrary paths outside those components.
Without `--host`, neither mode captures the host-wide vault or host config.
Env files inside the selected project are project data and are
included; host env files outside it are not read.
Workspace credential modes/policies are retained. A full workspace
backup can include config/auth values already persisted inside the workspace,
even though normal vault forwarding itself does not write them to config.

Default encryption asks for a new/confirmed masked backup passphrase (default
minimum 8 characters), separate from the vault password. Set
`backup_min_passphrase_length` in host oc-box config or use
`--min-passphrase-length N` for this backup. Both accept integers from 8 to 1024;
longer passwords are allowed up to 1024 UTF-8 bytes. Blank/short/mismatched input
is rejected, rather than asking age to generate a password. oc-box passes the
validated password to standard age through a private, echo-disabled terminal;
no password enters argv, environment variables, or plaintext files. Age still
provides the cipher/KDF and prompts directly when unlocking existing backups,
which are not restricted by the current creation minimum.
For unattended encryption use a native age X25519 public recipient, repeatable:

```sh
age-keygen -o ~/.config/backup-age-key.txt
age-keygen -y ~/.config/backup-age-key.txt
oc-box backup --project ~/devel/demo --recipient age1PUBLIC_KEY \
  --output /backup/demo.ocbox
```

The generated private key file is sensitive and should remain 0600 outside
project/container access. The public recipient may be stored in a scheduler.
Keep the private identity/backup passphrase separately; losing it makes the
backup unrecoverable. Age plugins/SSH identities are not accepted in format v1.

The envelope contains fixed-name age-encrypted components plus an encrypted
manifest and age-protected per-backup private key. The manifest authenticates
component SHA-256 digests/sizes; all components use age authenticated streaming
encryption. Plaintext streams through pipes, and secret keys/captured stderr use
anonymous memory. Temporary backup staging contains ciphertext only. At most
16 TiB ciphertext/expanded archive bytes per component and 2 million entries
are accepted in format v1. The output parent must exist outside the project,
registry, and private home, on a Linux filesystem that supports unnamed
temporary files (`O_TMPFILE`) and linking them via procfs. New output is
published atomically without replacing an existing file and has 0600 permissions.
The launcher opens the unnamed output before stopping a running workspace.
Expect enough free space for staged ciphertext, final output, and a temporary
committed image. If publication cannot be confirmed after linking, a complete
ciphertext file may remain: inspect it with `backup-info` before retrying. The
source remains stopped, not reset/deleted. The temporary capture image is
removed by its unique tag; shared base images remain.

### Include host settings for migration

On supporting stable and development releases, `backup --host --output FILE`
creates a host-only encrypted
archive, without a workspace or Podman. Add `--project PATH` or `--workspace ID`
to include these settings in a full workspace backup; `--portable` selects
a workspace plus host settings without the container OS image.

The host component includes exact file bytes for oc-box's host `config.json`,
the existing encrypted vault, the native OpenCode user configuration tree, and
native legacy `auth.json` and `mcp-auth.json` when present. It uses the current
XDG configuration and data roots. Keep host settings and credentials idle
while capturing them; the files are not snapshotted as one transaction.
Files outside those roots, symlinks, special
files, nested mounts, runtime state and the native host database are not
captured. `.git`, `.cache` and `node_modules` directories in the profile are
skipped; other unsupported entries are refused instead of silently omitted.
Limits are 128 files, 64 directories, 800 KiB for configuration and legacy auth
combined, and 2 MiB for the encrypted vault.

Add `--opencode-auth-export FILE` for native V2 provider and MCP login records.
Supply the native `opencode auth export --standalone` output in a private
regular file (0600) outside every archived directory, or pass `-` to read it
directly from a pipe without a plaintext export file. This is explicit: the backup does not start OpenCode or
open the native database itself. Only run the export with a trusted native
configuration and keep other writers idle.

```sh
oc-box backup --host --output /backup/host.ocbox
set -o pipefail
opencode auth export --standalone | oc-box backup --host --project ~/devel/demo \
  --opencode-auth-export - --output /backup/demo-and-host.ocbox
```

Treat this archive and its age identity/passphrase as sensitive. The vault
remains separately encrypted with its original passphrase. This is neither a
whole-machine backup nor a credential revocation mechanism.

### backup-info

Decrypt/verify a backup's manifest and ciphertext checksums without starting
Podman or extracting workspace contents. Requires `age`, `age-keygen` and
Linux anonymous-memory file support. Use a controlling terminal for passphrase
backups or a native private `--identity` file for recipient backups.
Default output (or explicit `--json`) is JSON metadata including kind, source
path/ID, architecture, component
sizes/checksums, saved session IDs, settings, and documented limitations. Raw
additional Podman option values are omitted to avoid printing saved secrets.
It never prints archived file contents or the host vault and does not execute
container image code. Metadata itself may contain private paths/session names.
Host-only inspection reports the archive type and version, not filenames from
the host profile, setting values, or credential records.
Use `--table` for a bounded field/value view of supported archive metadata.
It omits raw Podman values and unknown manifest fields.

```sh
oc-box backup-info /backup/demo.ocbox
oc-box backup-info /backup/demo.ocbox --table
oc-box backup-info /backup/demo.ocbox --identity ~/.config/backup-age-key.txt
```

Inspection stages only encrypted envelope components while checking them.
It authenticates the manifest and binds component ciphertext to it; restore
also verifies every age data stream through its final authentication chunk.
Wrong keys, corruption, missing/extra components, unsupported versions, or unsafe
outer envelope paths are rejected before restore activation.

### restore

Streaming failures identify the producer/consumer exit code and a fixed category
(for example exhausted storage, permission denial or archive rejection); private
tool stderr is never printed. Full image load also needs Podman's host temporary
image space (normally `/var/tmp`), in addition to destination image/private-home
storage. Backup/restore subprocesses honor an explicit `TMPDIR` so host temporary
image files and encrypted-component staging can use a suitable filesystem:

```sh
TMPDIR=/var/tmp oc-box restore /backup/demo.ocbox --project ~/devel/demo-restored \
  --identity ~/.config/backup-age-key.txt
```

The directory must exist and be usable by your regular user. This changes
temporary placement, not Podman's image-store location or its storage quotas.
Filesystem-wide free bytes do not establish user/project quota headroom.
Keep the encrypted source archive after failure and use a new absent
destination after incomplete-restore cleanup; no failed staging is activated.

Restore into a **new, nonexistent** host project destination. Never overwrites
an existing directory or workspace; there is no --force/--replace mode. Run on
the host as your regular user after installing age. The active source workspace
does not need to be removed: this is a fresh independent environment.

```sh
oc-box restore /backup/demo.ocbox --project ~/devel/demo-restored
oc-box restore /backup/demo.ocbox --project ~/devel/demo-restored \
  --identity ~/.config/backup-age-key.txt
```

It decrypts/verifies the manifest, checks architecture/settings, stages project
files in a private sibling directory, creates a fresh labeled home volume, and
streams its extraction under podman unshare for container UID/GID mappings.
Registry content is retained as `recovered-registry/` evidence, not activated as
arbitrary create commands or old host-archive paths. Then a full backup loads
the captured OCI image; portable mode selects the original distro catalog image.
Only after component restoration succeeds does atomic no-replace rename publish
the project and record its actual destination identity with fresh IDs.

The new environment is **created but not started**. Its resume selection keeps
the original conversation IDs and complete container DB. Original private home
and in-container project paths remain the same; `container_project` in metadata
can differ from the new host project. Existing session snapshots/tool paths need
no database rewrite. Status reports both paths when different. Host archive
sync from a restored workspace uses fresh namespaced archive IDs and the new
host project. Old host archive associations are not reused/deleted.

Normal startup still copies the current host config/auth and checks the exact
host OpenCode version. The archive's device/extra Podman settings are recorded
as requirements but **not automatically activated**. The restored image's
environment and declared volumes are cleared, image healthchecks are disabled,
and a fixed entrypoint is used;
backup image hooks, startup commands, external mounts, and host device settings
are not trusted as new host launch privileges. Full restores retain OS files,
which are executable code when you later start/use that environment: restore
only trusted backups. Portable restores recreate baseline tools on first normal
launch and need custom system dependencies reinstalled. Host drivers and CPU
architecture are not portable; this initial format requires a matching CPU
architecture even for portable mode because cached home binaries may be native.

Extraction refuses traversal, duplicate/excessive entries, special devices/
FIFOs/sockets, sparse extensions, oversized metadata, hardlinks outside earlier
regular entries, and any write through a symlink ancestor. Project files become
owned by the current host user; private-home ownership is mapped in the rootless
namespace. Regular modes/timestamps/symlinks/hardlinks are kept, but setuid/setgid/
sticky bits, ACLs, xattrs, and source SELinux labels are not restored. New project
mounts use Podman's normal :z relabeling. Symlink targets may name outside paths
but are never followed during extraction; check them before later host use.
Restored project and staging roots are kept private (0700), even if the original
root had broader permissions; permissions of ordinary files/subdirectories remain.

The workspace restore does not unlock a vault or by itself install archived
host settings. Workspace credential modes/policies are retained, with selections
stored as names only. A restored default/global V2 workspace uses the restoring
host's current vault. Restore archived host settings explicitly as described
below, or provide matching host env/vault values before normal startup.
Outside env files are excluded.
Copied auth/config and secrets stored in project/home/OS are part of
the encrypted payload and remain present after restore. A failed operation
cleans only its verified new project/volume/container/registry and preserves
original environments. If cleanup cannot verify staging or resource ownership,
it retains a blocked incomplete registration for host inspection instead of
deleting unverified data. Loaded OCI images
may remain in shared Podman image cache after failure; no global prune is used.
The host registry journals fresh resource IDs and `restore_staging_project`
before payload extraction. An interrupted or failed-cleanup entry has
`restore_incomplete: true`; list/status can expose its ID without launching it.
Inspect the staging location, use plain `purge --workspace ID` for its managed
resources (which skips history sync for incomplete restores), and retry to a new
destination. Orphan staging directories may need manual host removal after
inspection; the launcher never deletes a caller-supplied tree on marker alone.

### Restore host settings on a new machine

Install oc-box, age and the required OpenCode version on the destination host;
install rootless Podman for a workspace restore. Stop OpenCode and other writers,
and use fresh destination paths. Host restore never replaces an existing target,
even if it is empty or appears identical. In particular, do not launch OpenCode
on the destination before restoring a native profile and its fresh V2 store.

```sh
oc-box restore /backup/host.ocbox --host
oc-box restore /backup/demo-and-host.ocbox --host --include-opencode \
  --project ~/devel/demo-restored
```

`--host` restores oc-box settings and the encrypted vault, without unlocking
it. `--include-opencode` additionally requires typed consent on the controlling
terminal before restoring native files and any supplied native V2 export.
Restored settings can enable forwarding, updates or system-journal mirroring.
Restored OpenCode configuration and plugins can execute code or change
permissions on the next launch. Only restore trusted archives. Native V2 import
tries any stable OpenCode 2.x release. It requires a Linux `/dev/shm` tmpfs,
no active managed OpenCode config, and a fresh credential destination. Before
publishing, it verifies the native import/export round trip and that the new
database contains no unexpected saved work. An incompatible native release
fails without installing a database; an existing database is never merged.
Credential validity on a new host is not guaranteed.

Host files are published without overwrite, privately, one file at a time.
This requires a Linux filesystem supporting anonymous temporary files and
no-overwrite links. Workspace and host publication are not one transaction: if
host activation fails after the workspace restore, the restored workspace
remains stopped and successful host changes are reported rather than rolled back.
Host XDG location references, external plugin dependencies, shell environment
files, hardware/device mappings and installer-specific state are not remapped
or activated. A full archive with the host component preserves the supported
settings without re-entering them, not every external dependency.
Host registries, activity journals, dashboard favorites and per-workspace UI
preferences are not activated from the archive.

## Options

### -h, --help

Use `oc-box --help` for a short index of commands grouped by purpose. Use
`oc-box COMMAND --help` to see the syntax and options for one command.
Launcher commands group only the options that apply to that command; commands
with dedicated help pages keep their existing layouts. `-h` is the short form
in both cases. Startup still defaults to `start` when no command is given;
use `oc-box start --help` for startup options.

For example, `oc-box start --help` shows `--journald` and `--no-journald`,
which control whether that launch mirrors activity events to the system
journal. `oc-box logs --help` shows the reader's `--journald`, which selects
existing mirrored entries instead of the private journal. It does not enable
mirroring.

Help does not require or look up a workspace, take locks, access the vault,
call Podman, or read or write launcher state, including the activity journal.
For full option semantics, defaults, and examples, use this command reference
or the installed `man oc-box` and `info oc-box` manuals.

### --min-passphrase-length N

Set the minimum character count for a new vault or backup passphrase for this
operation. Accepted only with `oc-box secrets init`, `oc-box secrets passwd`,
and `oc-box backup`; it is incompatible with backup `--recipient`. N must be an
integer from 8 to 1024. The default is 8 unless the corresponding host config
key `vault_min_passphrase_length` or `backup_min_passphrase_length` is set; this
flag overrides that key without saving it. Passwords may exceed the minimum,
subject to the 1024 UTF-8-byte limit. Unlock/restore never enforces a new creation
policy against an existing password. The number is public policy, not a way to
pass a password in argv. For example:

```sh
oc-box secrets init --min-passphrase-length 8
oc-box secrets passwd --min-passphrase-length 16
oc-box backup --project ~/devel/demo --output /backup/demo.ocbox \
  --min-passphrase-length 12
```

### --feature FEATURE

Doctor-only, repeatable. Limit the dependency report and exit-status evaluation
to named features. `oc-box doctor --feature backup-info` checks age and memory
support without requiring Podman or host OpenCode. With no selector, report all
features and evaluate launch prerequisites for the exit status. Use
`oc-box doctor --help` for the complete accepted names.

### --maintenance

Terminal-only. Open the restricted maintenance shell instead of a live V2
session client. This retains the existing allowlisted environment, no host
credential forwarding/unlock, no config/auth refresh, no startup profiles and
no persistent shell history. Maintenance holds the exclusive workspace lock;
exit the active owner first. Without this flag, terminal joins an active V2
session's actual persistent shell PTY; 1.x continues to use
the existing maintenance shell.

### --read-only

Tui-only. Disable session changes, terminal attachment/control, backup
creation/restore, workspace removal and host logging saves. The dashboard
does not delete archive files in any mode. Inspection
and refresh remain available. Explicit native host Connect may still start
the normal host service; this flag is not an offline/service-free mode. Opening
the dashboard itself does not start a workspace or unlock credentials.

### --demo

Tui-only. Use synthetic data, force read-only mode and ignore workspace selectors.
No host service, real registry, journal or workspace action is used. A demo may
run inside a container but still requires optional TUI dependencies and an
interactive terminal. It is not live host acceptance.

### --port N

Web-only. Select the host-loopback relay port: 0 (default) chooses a free port;
explicit ports are 1..65535. Binding failure is reported without replacing an
existing listener. The container service's own random loopback port stays private.

### --no-open

Web-only. Print the native one-use browser pairing link instead of invoking the
host browser. The foreground relay still runs until Ctrl-C or workspace stop.

### --session ID

With `web`, explicitly display an existing native session from this same private
workspace service. It overrides URL-selected and reported TUI initial selection,
never creates a session or adds a fake open tab, and is not itself proof of TUI
focus. A selected child retains its exact conversation route; its verified root
is highlighted separately. See `web` for membership and unavailable-report rules.

With `attach`, required in both direct and broker modes. Select the exact native
target session, for example `--session ses_example`, at the running workspace's
native project directory. A workspace ID, host archive association or session at
a different/nested directory is not sufficient. Observed archived sessions and
staged native reverts refuse; no default session is inferred.

### --broker

Attach-only. Keep a foreground host-terminal client-MCP broker open for explicit
`request_image` proposals and one-use `read_attachment(id)` image retrieval.
Every file requires independently typing `attach` in the host terminal.
Incompatible with positional PATH, `--text`, `--send` and `--acknowledge-risk`.
It does not intercept native paste/drop, browse folders or grant live file access.

### --text TEXT

Direct attach-only. Set the text of the native image user message. The default
is `Please examine this host image.` This is not a draft prompt or an instruction
for the host broker. Forbidden with `--broker`.

### --send

Direct attach-only. Request native queueing and `resume: true` for the image user
message; omission requests `resume: false`. Active native execution can still
consume queued input without this flag. Neither setting guarantees stopped
inference or creates a draft attachment chip. Forbidden with `--broker`.

### --acknowledge-risk

Share and direct attach-only. Replace typed host disclosure confirmation for
explicitly authorized noninteractive use, without suppressing the warning.
This is not blanket native permission approval. Forbidden with `attach --broker`,
where every file always needs independent typed host approval.

### --project PATH

Select a project directory; for `restore`, this instead names a new, nonexistent
destination. Relative paths resolve from the host working directory. Quote paths
containing spaces. For lifecycle commands and `preferences`, the default is the
current directory. For status, omission means all workspaces.
For `tui`, it initially selects a registered project; omit both selectors
for the registered workspace overview. `tui` accepts only one selector.
For `logs`, it filters journal entries by a registered project path, cannot be
combined with `--workspace`, and does not default to the current directory.
The project must not be the host home, `/`, `/home`, the oc-box registry, or a
directory containing trusted launcher/state files. Colons, commas, and line
breaks in the project path are unsupported because of Podman mount syntax.

```sh
oc-box --project "$HOME/devel/my project"
```

### --workspace ID

Select an existing workspace by its full 32-character ID from `oc-box list`.
This works from another directory and allows cleanup after the project is
deleted. Starting, syncing, or resetting still needs its original project
directory. The identity marker and registry are not interchangeable with
OpenCode session IDs. Example: `oc-box --status --workspace WORKSPACE_ID`.
For `logs`, this is an exact workspace ID filter and cannot be combined with
`--project`.

### --status

Report CPU, RAM, storage, state, limits, and sessions without opening OpenCode.
Equivalent to `status`. Only selectors and `--json` or `--table` apply. It cannot be combined
with another command, session mode, resource change, or deletion action.

### --json

Emit status as one JSON object with `sampled_at` and `workspaces`. Sizes are
numeric bytes and unavailable values are `null`; notes explain partial results.
Progress goes to stderr, not the JSON stream. Doctor uses its own versioned
feature/dependency schema. `logs` instead emits JSON Lines, one event per line,
not a single JSON object or array.
`preferences` reports one object with the effective workspace UI settings on a
read or a save; `--json` does not stop explicit preference changes from saving.
`backup-info` already defaults to JSON. `--json` and `--table` are mutually exclusive.

```sh
oc-box --status --json > status.json
```

### --table

Select human-readable tables instead of JSON or the usual text. Available with
`status` (including `--status`), `logs`, `preferences`, `doctor` and
`backup-info`. It uses the same source, filters and exit status as the
corresponding report. Status and backup inspection tables show bounded
metadata and omit saved Podman option values. It is mutually exclusive with
`--json` and does not apply to `logs --path`.

```sh
oc-box backup-info /backup/demo.ocbox --table
oc-box logs --follow --table
```

### --level debug|info|warn|error

For `logs`, set the minimum severity displayed, from `debug` (all levels) to
`error` (errors only). Default: `info`, or all levels with `--tail`. This is a read filter; it does not change
what gets journaled or what an owning launcher prints.

### --limit N

For `logs`, set the maximum number of matching retained entries in the initial
output, from 1 to 10,000. Default: 100, or 10,000 with `--verbose=ALL`.
Rotation may leave fewer entries available.

### --follow

For `logs`, keep displaying newly retained matching entries after the initial
output. Follow is best-effort: entries rotated out before they are read cannot
be recovered. Stop following with Ctrl-C.

### --tail

Show recent environment preparation tasks and their results instead of general
activity. Add `--follow` to watch new preparation records live. This is
read-only, uses a separate ring under the same private log directory (including
any `--log-dir` override), and
does not show raw installer streams.

### --journald

For `start` (including when the command is omitted), `reset`, `stop`, `sync`,
`terminal` and `web`, enable best-effort system journal mirroring for this
invocation, overriding the saved `logs.journald` choice. The private journal
remains primary. Do not combine with `--no-journald`.

For `logs`, read this user's mirrored activity events from the local system
journal instead of the private file. Requires a usable local `journalctl` and
access to those records. It does not enable mirroring, change journal settings
or fall back to the private file.

### --no-journald

For the same launcher commands as `--journald`, disable mirroring for this
invocation even if saved `logs.journald` is `true`. This does not disable
private journaling or change the saved choice. It does not apply to `logs`.

### --log-dir PATH

Override the saved `logs.directory` for this invocation of `start` (including
when the command is omitted), `reset`, `stop`, `sync`, `terminal`, `web`
or private `logs`. `PATH` is a directory, not a logfile. It must be absolute
after `~` expansion and cannot be `/` or include a `..` path component. An
append may create the directory privately; a read, including `logs --path`,
never does. Old logs are not moved or deleted. Do not combine with
`logs --journald`.

### --lifecycle-diagnostics

Show expanded launcher stage messages and fixed-category diagnostics for this
invocation of `start` (including when the command is omitted), `reset`,
`stop`, `sync`, `terminal` or `web`. Quiet output is the default and there is
no saved diagnostic setting. This option does not change native OpenCode output,
journaling or lifecycle results. See [Launcher diagnostics](#launcher-diagnostics).

### --verbose[=LEVEL]

For `start` (including when omitted), `reset`, `stop`, `sync`, `terminal` and
`web`, emit validated activity diagnostics on stderr at the selected minimum
severity: `debug`, `info`, `warn`, or `error`. Levels are case-insensitive.
Activity diagnostics are buffered and may appear at command exit.
Bare `--verbose` means `--verbose=ALL`; both include every severity. With
`info`, `debug`, or `ALL`, the native TUI startup
notices also explain the enabled host-import features. Bare `--verbose` and
`--verbose=ALL` additionally restore the selected preparation tools' live
stdout and stderr for that invocation. Named severity levels alone keep those
streams quiet. Live installer output can contain sensitive details; avoid
recording or sharing it. Raw tool output still never goes to the activity
journal. The option does not suppress launcher warnings, refusals or prompts,
alter the interactive native OpenCode output, or change what gets journaled.

For `logs`, this is a read filter rather than a launcher setting. Use it instead
of `--level`; `ALL` includes all validated retained records within the read
bounds. It works with `--follow`, `--json` and the same explicit source choices.
It never changes the verbosity of an owning launcher; put `--verbose` on
`start` or `reset` to display live installer output.

### --path

For `logs`, display the effective private `activity.jsonl` pathname without
reading or creating the journal. Use `oc-box logs --path` alone or with
`--log-dir PATH`, not with `--journald`, workspace filters, `--follow`,
`--tail`, `--verbose`, `--json`, or `--table`. `--level` and `--limit` have no effect
when showing a path.

### --about

Print oc-box version, author Paulo Dias and email, OpenAI coding-assistant
contribution, GPL-3.0-or-later licensing, and source commit. No Podman or
workspace is required. Use alone, without lifecycle selectors/options.

### --version

Print oc-box's commit-derived version and exit without Podman. Stable releases
use `1.8.N+gHASH`; development builds use `1.9.N.dev0+gHASH`. N is the
full-history commit count minus 29 (the original 1.0.0
baseline). Each later commit advances N; shallow history cannot supply a release
identity. Tracked uncommitted changes add `.dirty`. Untracked files do not. Installed
copies retain their installation-time identity in `build-info.json`; reinstall
to update it. This is separate from the host OpenCode version matched inside
containers. Use alone, for example: `oc-box --version`.

The OpenCode V2-compatible 1.5 series, 1.6 refactoring series and 1.7 manager/sharing
series retain the same commit-derived patch count; a first published patch is
not required to be zero. Installed older copies keep their original
0.1/1.0/1.5/1.6/1.7 identity.
PyPI distribution metadata uses `1.8.N` for stable releases and `1.9.N.dev0`
for development releases, because public PyPI versions
cannot contain the local `+gHASH` suffix. Its packaged build-info still includes
the full source identity displayed by this command, so the exact Git commit is
retained even in a wheel/source distribution installed without Git.
Existing 0.1/1.0 installations retain their original embedded identity until
upgraded through their usual channel. The absolute commit count remains in
metadata for upgrade ordering; no workspace reset or database migration is
required for the version-series transition.

### --distro NAME

Choose a supported distro for a new environment. The default matches the host
ID when supported; openSUSE Tumbleweed/Slowroll use Tumbleweed, other openSUSE
variants use Leap, and unknown hosts fall back to Tumbleweed. The image is a
distribution userland, not an exact clone of host packages or snapshot date.
Changing an existing workspace requires `reset --distro NAME`.

```sh
oc-box --project ~/devel/demo --distro tumbleweed
oc-box reset --project ~/devel/demo --distro debian
```

### --check

With `upgrade`, preview the saved or explicitly selected published channel
and validate support for requested `--vault`/`--tui` extras without installing
or persisting channel/extra preferences. Returns success when a version is
available or the installation is current; it is not an update-available exit
code. Network errors or unsupported requested extras return nonzero. Cannot be
combined with `--auto-update`, workspace selectors or other commands.
Example: `oc-box upgrade --devel --tui --check`.

### --stable

With `upgrade`, select and remember the stable channel after a successful
verified upgrade. With `--check`, preview only. Mutually exclusive with `--devel`;
cannot be combined with `--auto-update` or workspace actions. An explicit
downgrade replaces launcher files, not workspace/session state.
Example: `oc-box upgrade --stable`.

### --devel

With `upgrade`, select and remember published development prereleases after a
successful verified upgrade. With `--check`, preview only. Mutually exclusive
with `--stable`; cannot be combined with `--auto-update` or workspace actions.
Development features remain experimental; selection is not stable acceptance.
Example: `oc-box upgrade --devel`.

### --vault

With `upgrade` only, add optional vault/cryptography dependencies through the
verified installation owner. Independent of `--tui`, additive to existing extra
requests, and compatible with `--stable`, `--devel` or nonmutating `--check`.
Without a selector, use the saved channel. The same release can gain support;
repeating a satisfied request is a no-op. Does not select a workspace credential
policy, initialize/unlock a vault, or deliver credentials. Requires a launcher
release supporting this flag and a published target supporting the extra.
Cannot be combined with `--auto-update` or workspace actions.
Example: `oc-box upgrade --devel --vault`.

### --tui

With `upgrade` only, add optional dashboard dependencies through the verified
installation owner. Independent of `--vault` and additive; may be combined with
either channel selector and nonmutating `--check`. An explicit request against
a target without the dashboard, including older stable 1.6, refuses. An inherited
TUI preference survives for later releases that include it. The same release can gain dependencies; repeating a
satisfied request is a no-op. Does not launch the dashboard or a workspace.
Requires a launcher release supporting this flag and a published supporting
target. Cannot be combined with `--auto-update` or workspace actions.
Example: `oc-box upgrade --devel --vault --tui`.

### --auto-update on|off

With `upgrade`, persistently enable or disable automatic updates before normal
installed launches. The default is off. This setting itself performs no download
or installation and preserves other keys in `~/.config/oc-box/config.json` (or
its XDG equivalent). GitHub updates follow the saved channel; package automatic
updates remain disabled. Cannot be combined with `--check`, `--stable`,
`--devel`, `--vault`, `--tui` or workspace actions.
Example: `oc-box upgrade --auto-update on`.

### --new

Select a new root conversation. `start` keeps the existing environment; `reset`
still replaces its container OS before launching. An additional client cannot
use this option against an existing V2 owner. Mutually exclusive with `--resume` and
`--import-session`. Example: `oc-box --project ~/devel/demo --new`.

### --resume

Require a registered workspace and select its recorded eligible root rather than
the default saved-tab selection on supported V2 launches. If no eligible root
exists, OpenCode can start a new conversation. Use with start/reset. Mutually exclusive with `--new` and
`--import-session`. Example: `oc-box --project ~/devel/demo --resume`.

### --import-session SESSION_ID

Copy a host conversation from the selected project into the private container
database, verify the transcript, select the exact ID, and launch it. Start-only;
mutually exclusive with `--new` and `--resume`. Quit the source host client first
to include its final saved turn. A subagent session cannot be imported as a root.
The original host conversation is retained. See Sessions and recovery.
An oc-box host archive can also be imported into a fresh environment for that
same project; the original registry and saved archive verify its project.

```sh
oc-box --project ~/devel/demo --import-session ses_HOST_SESSION_ID
```

### --model PROVIDER/MODEL

Select a model for the native launch. On V2 this can set the model on the chosen
native session, but it does not save an oc-box workspace-wide default. Use with
start/reset; an additional client cannot set a model on an existing V2 owner.

```sh
oc-box --project ~/devel/demo --model github-copilot/gpt-6.1-sol
```

### --image-preview

V2-only, for an **owning** start (the default command) or reset TUI launch.
Set both `prompt.image_preview` and `session.image_preview` to `true` through
`OPENCODE_CLI_CONFIG_CONTENT` for interactive full native TUI clients belonging to
that owner. This overlay contains only those two image booleans: it does not set
permissions, notifications or other preferences. These are terminal preferences,
not `opencode.json` server settings. The current native V2 settings reference is
at <https://opencode.ai/docs/cli/config>. A supporting native V2 release and host
terminal are required; actual rendering on older releases is not established.
This option does not install another OpenCode version or change permissions.

The persistent container `cli.json` remains the base. Existing container-local
preferences and their original image values survive refreshes; a missing file is
seeded from the validated host snapshot, without forced image values. JSONC
comments, CRLF and trailing commas are retained in that base snapshot. The host
file and host OpenCode configuration are never edited.
Malformed/non-object input or non-object prompt/session sections are refused,
not replaced. Symlinked paths, nonregular/hard-linked CLI files, foreign-owned
config directories/files, and group/world-writable directories/files are refused.
Validation runs before initial workspace registration or reset resource removal.
The validated CLI bytes stay in launcher memory through credential prompts and
the owning launch/reset. That exact snapshot supplies any needed first-use seed:
edits to host `cli.json` while a credential prompt is waiting are not reread after
reset has removed resources. They can apply to a later first-use seed, not
overwrite an already saved container preference file.

Use `oc-box preferences --image-preview on|off` to retain the workspace choice
for future owning native V2 start/reset launches. It is off when not saved.
`--image-preview` and `--no-image-preview` override the saved choice for one
owning launch without changing it. Saving unrelated native UI settings during
an enabled generation does not persist the forced image values. Additional
full TUI clients inherit the actual running owner's override and cannot supply
either flag while it is running. API/history clients and session-shell terminals
receive no overlay. Saved enables are not applied to V1. Web,
terminal/maintenance, informational commands and history sync ignore the
saved choice and reject the flags. Only the boolean request is saved in host
workspace preferences; no CLI snapshot is kept there.

```sh
oc-box preferences --project ~/devel/demo --image-preview on
oc-box --project ~/devel/demo --image-preview
oc-box reset --project ~/devel/demo --image-preview
```

### --no-image-preview

Skip the launcher image-preview override for this owning TUI start/reset, even
when the workspace preference requests it. It does not force image previews
off in native OpenCode's own `cli.json` or change the workspace preference.

### --host-file-open

Request Ctrl-click host file opening for this native `start` (including the
implicit command) or `reset` TUI client. Applies to an owning client or an
additional `start` client; it does not change a running owner's settings. A
positive request requires interactive native OpenCode 2.3.x under direct
Konsole or Yakuake and is incompatible with custom Podman arguments.
If actual mount or desktop admission later fails, host opening stays off.

### --no-host-file-open

Disable Ctrl-click host file opening for this native `start` or `reset` TUI
client, even if the workspace preference or installation default is on. Neither
flag is saved or changes another client.

### --startup-logo ascii|graphics

For `preferences` only. Save the startup display for future native `start`
and `reset` clients of a registered workspace. `ascii` always uses the `[/]`
mark on the loading line. `graphics` (the default) requests a one-row PNG in
direct Kitty or Konsole 22.04+ (including Yakuake using that Konsole part).
It falls back to ASCII
when the terminal is unsupported or the launcher detects conflicting markers,
SSH or a multiplexer. The PNG disappears before native output begins; the
option does not affect web, the manager, or terminal shell clients.
Terminal detection uses compatibility hints, not proof of the emulator.
From a source checkout, `python3 tools/demo_startup_logo.py ascii` and
`python3 tools/demo_startup_logo.py graphics` preview the loading line without
launching a workspace or saving preferences.

### Experimental Ctrl-click host file opening

With no saved workspace choice, the current development-channel GitHub
bootstrap defaults this feature on
only when it verifies a fresh installation. Existing installs, upgrades and
older bootstrap scripts do not acquire a new default;
later upgrades can retain a previously established default. Without verified
install evidence, including ordinary PyPI `pipx`/`uv`/`pip` installs and source
checkouts, the default is off. Automatic mode skips unsupported clients and
does not opt a restored workspace in. Neither ordinary launch nor a host
settings restore creates a fresh-install default. The former environment
switch is no longer used.

Set it for future native TUI clients, or override it for one client:

```sh
oc-box preferences --project ~/devel/demo --host-file-open on
oc-box start --project ~/devel/demo --host-file-open
oc-box start --project ~/devel/demo --no-host-file-open
```

**Ctrl-left-click** a file link in an assistant reply to open a private local
snapshot in the host's associated application. It must be an actual link, for example
`[Notes](/tmp/notes.md)`; unlinked paths and paths shown as inline code are not
clickable. Absolute paths and local `file://` URIs resolve inside the container.
Relative paths resolve against the active session's directory, not the host
shell's directory. Hold Ctrl until after releasing the mouse button. Each
successful click reads a fresh file; there is no panel or confirmation step.

- PNG: opens a normalized static image, retaining the resolution and alpha but
  not embedded metadata. Input is limited to 4 MiB and 4,194,304 pixels
  (up to 4,096 per side). The normalized image must also fit within 5 MiB.
  Animation and interlacing are refused.
- TXT and Markdown: opens the UTF-8 snapshot, up to 256 KiB, in the host's
  associated application. Markdown is not sanitized like HTML; a Markdown
  viewer can render active content or fetch resources. Use a plain-text
  editor for untrusted drafts.
- HTML: opens a safe static copy of the article structure and text in the
  browser. Scripts, styles, links and remote resources do not carry over.
  This is not a test of the original page's appearance or behavior.

Other formats, special files, filesystem links, unsafe paths, changing files
and unexpected mounts are refused. Custom Podman arguments disable the
experiment. Use direct Konsole or Yakuake with mouse reporting enabled.
Kitty is not supported by this experiment: its optional terminal remote control
can inject raw input. The host must have trusted `/usr/bin/xdg-open` and
`/usr/bin/xdg-mime` and a private `XDG_RUNTIME_DIR`. Incomplete mount inspection
disables host opening. The check covers the current local Podman store and
registered projects; it is not a census of other engines or atomic with
container starts. If a stopped container references a missing mount source,
oc-box checks the path and its nearest existing parent for overlap. Missing
active sources, dangling links, and inaccessible sources still block host
opening. Do not mount workspace data onto the host runtime or staging
directory; this check does not prove isolation for arbitrary host bind layouts.

The host relay grants at most one bounded transfer per observed Ctrl-click.
OpenCode chooses the link target; opt in only for a workspace whose programs
you trust to honor that click. A rendered link or container output alone does
not mint a grant. No new listener or share is created, no file is written
back, and no clipboard or model access is granted. The host copy survives the
asynchronous viewer handoff. The launcher keeps up to 128 copies and 64 MiB
in total. It clears copies older than 24 hours on a later open rather than
evicting recent copies to make space. The desktop runtime-directory policy can
remove them sooner. Copies rejected before calling the opener
are removed immediately. Older experimental staging directories are not
adopted or cleaned by the new launcher.

Neither switch changes host OpenCode or terminal settings.
If the existing owner was started before upgrading oc-box, exit it normally and
start it again so the container gets the updated plugin. This is a private
native renderer integration that may stop working after a 2.3.x update or in
terminals that do not pass Ctrl-click to OpenCode. Leave terminal hyperlink
handlers at their normal settings; this feature does not use them. It is not available in web,
V1, plain host OpenCode, or the maintenance/session terminals.

### --host-drop

Experimental and **enabled by default per workspace** for supported native
OpenCode V2 clients. Any valid 2.x minor/patch release is eligible; there is no
exact patch allowlist. V1, malformed versions and other major series refuse.
Plain `start` (including the default command)
or `reset` reads the saved host preference. `--host-drop` saves enabled and
requires a supported client; `--no-host-drop` saves disabled. The flags are
mutually exclusive. An additional `start` client may attach to an existing owner
without restarting it, refreshing credentials or changing native configuration.
It reads the preference for its own relay; **existing clients keep their setting**.
Running another `oc-box` opens another TUI, not a live toggle.

Automatic/default mode skips importing on unsupported terminals, native versions
or noninteractive clients rather than preventing ordinary startup. Explicit
`--host-drop` remains strict and refuses V1; `--no-host-drop` may save an opt-out
for V1 without activating any bridge. Importing never activates for `web`,
`terminal`/maintenance, the `tui` dashboard, sync or informational commands;
those commands reject both startup flags.

Use a direct interactive host TTY, without SSH, tmux (`TMUX`) or screen (`STY`).
The experimental profiles are direct Konsole and Kitty. Exactly one host
compatibility marker (`KONSOLE_VERSION` or `KITTY_WINDOW_ID`) must identify the
profile; unknown or inherited ambiguous setups refuse. These markers are an
operator-trusted compatibility selection, not cryptographic emulator identity.
The pathname bridge accepts a complete qualifying paste. Konsole uses that bridge
only, with no OSC 72 probes/registration and unchanged child output. The Kitty
profile enables structured OSC 72 drops only after an affirmative capability reply.
Kitty graphics support, including Konsole's graphics support, is not proof of
drag-and-drop capability. This is not a guarantee about every emulator/version.

Compatibility is checked through the existing private V2 client connection,
bounded transfer-helper handshake/replies and terminal framing, rather than
rejecting a new 2.x version number. These checks fail closed when their observed
contracts break. Native image composition has no public capability negotiation:
oc-box cannot automatically prove that every future TUI still converts pathname
pastes into attachments. A native refusal or literal staged path instead of an
attachment should be reported, not bypassed by sending automatically. This policy
does not govern history, recovery, mirrored web tabs or other attachment modes.

Konsole's default direct URL-drop setting produces the required bracketed paste
while native OpenCode enables mode 2004. Keep **Disable drag & drop menu for files
& URLs** enabled in the Konsole profile. The optional menu's **Paste Location**
action sends raw, unframed input and is intentionally not intercepted. Bare
absolute names, quoting for spaces and the normal trailing space are accepted.
No timing or pathname-shaped keystroke guessing is used.

**A qualifying pathname paste anywhere in an importing-enabled TUI authorizes capture,
without a per-file confirmation prompt.** The bridge cannot prove a real desktop
drag gesture or composer focus. Do not paste a sensitive path merely to discuss
it. Model output, an MCP request or a proposed host path is never capture
authority. For separate host approval of every image, use the unchanged
`attach --broker` workflow instead.

The pathname bridge accepts only a whole paste containing one to four absolute
Linux paths, with supported quoting/escaping for spaces. Mixed prose, newlines,
traversal components, control characters and `file://` text expansion are
not imported; this is not shell evaluation. Only regular, single-link
PNG/JPEG/GIF/WebP/PDF files with matching extensions and signatures qualify.
Protected host/application/credential paths, links, special files and unsafe
source changes refuse through the sharing descriptor-pinning guards. Limits
are 8 MiB per file, 16 MiB per batch, and lifetime totals of 32 files/64 MiB per
client. Signature checks are not decoding, sanitization or a malware scan.
DOCX is unsupported; no verified reader or converter is supplied.

Captured bytes cross a private non-PTY stdin channel, not argv, environment,
shell commands or project staging files. They are staged in a private container
`/tmp/oc-box-drop-<token>` directory (0700), with generated files (0600).
The native TUI, server, plugins and tools still execute inside the container.
The host bridge feeds rewritten container paths back as native paste input:
it never appends Enter or submits a native prompt API request. Native composition,
attachment previews and sending remain native UI behavior. Optional
`--image-preview` can enable inline image visuals on a qualifying owning launch;
it is not required for this bridge and does not add host-read authority. A
selected model/provider may not support PDF or a particular attachment format.

Normal client exit attempts bounded cleanup of only this client's recorded files.
There is no background or restart cleanup scan. Crashes, interrupted cleanup,
open references, copies, native history and backups can retain disclosed bytes.
Container root and agents/tools using the same container user can read staged
files; this is not per-session isolation. Host input parsing excludes opaque
terminal responses. In the Kitty profile only, the output relay reserves its
C0 OSC 72 wire commands and fails closed on unsupported framing limits; valid
native graphics APC_G and keyboard CSI remain unchanged. This is not generic
C1-output reservation or a blanket terminal/security guarantee, nor a
new live-host acceptance result. See `SECURITY.md` and `docs/testing.md`.

```sh
oc-box --project ~/devel/demo --host-drop
oc-box start --project ~/devel/demo --host-drop
oc-box reset --project ~/devel/demo --host-drop
oc-box --project ~/devel/demo --no-host-drop
oc-box preferences --project ~/devel/demo --host-drop off
```

### --no-host-drop

Save host importing disabled for future workspace TUI clients and open this
client without the bridge. Mutually exclusive with `--host-drop`; neither flag
live-toggles existing clients. To change only the preference without opening a
TUI, use `oc-box preferences --host-drop off`.

### --host-clipboard

Experimental, **off by default, with a saved per-workspace host preference**.
On a supported direct Konsole or Kitty terminal, enable with `start` (the
default command) or `reset`, then press **Ctrl+Y** to import a PNG clipboard image
or copied local PNG/JPEG/GIF/WebP/PDF files. Ordinary Ctrl+V, terminal text paste,
clipboard contents and host OpenCode settings are unchanged. The native TUI still
runs inside the container; there is no Wayland/X11 socket mount or host clipboard
MCP endpoint.

`--host-clipboard` saves enabled for future native TUI clients;
`--no-host-clipboard` saves disabled. Omit both to use the saved choice, or off
when none exists. Existing clients retain their original setting. To save policy
without opening a client, use `oc-box preferences --host-clipboard on|off`.
The private host preferences survive stop/start and reset, but are not restore
authority for a fresh workspace. They store only booleans, not clipboard data
or a desktop helper path.

The host relay recognizes the legacy Ctrl+Y byte (0x19) and distinct ASCII CSI-u
or ModifyOtherKeys reports only at top-level host input, not inside bracketed
pastes or terminal replies. Printable `y`/`Y` text never grants a read. A
terminal/global shortcut that consumes the chord will not import content.
Ctrl+Y is unassigned in the documented OpenCode 2.x defaults checked on
2026-11-18; custom bindings and desktop shortcuts may still conflict. It replaces
the earlier experimental Ctrl+Alt+Shift+V binding, without editing native
keybindings or intercepting normal Ctrl+V. **Ctrl+Y is reserved throughout an
opted-in client, including shell panes, so its usual shell-yank action is not
available there.** Clients without effective clipboard importing retain their
native Ctrl+Y.
It inherits the direct-TTY, native-version and terminal-profile
requirements of `--host-drop`; SSH, tmux, screen, unknown/ambiguous profiles and
unsupported clients refuse explicit enable. A saved enable gracefully skips
clipboard importing on unsupported terminals, native V1 or when the selected
desktop helper is missing, without clearing the preference. Explicit enable
still requires strict preflight; it does not read the clipboard.
CSI-u repeat/release reports do not request another import; legacy
control-byte/ModifyOtherKeys repeats cannot be distinguished from new presses.

Clipboard access occurs only in response to the opted-in host input shortcut,
never from model output, a plugin/MCP request or background polling. The keyboard
report is host input authority, not cryptographic proof of a physical keypress
or composer focus. The shortcut authorizes import anywhere in that connection;
focus the native prompt before using it. Other workspace tools can read imported
bytes, and history, provider systems, backups, copies or crashes may retain them.
Read-only import is not confidentiality or recall.

Wayland requires `wl-paste` from `wl-clipboard` on the host. X11 requires `xclip`.
If Wayland is selected, a missing helper does not fall back to another clipboard.
Preflight only locates the helper; it reads no clipboard data. On the shortcut,
one bounded content-type query selects `text/uri-list`,
`x-special/gnome-copied-files`, or `image/png`, in that order. There is one payload
read under the same helper deadline and desktop-only environment. File lists
take precedence over an optional PNG thumbnail; refused/invalid file lists do
not fall back to another format. Type lists and file-list payloads are limited
to 16 KiB, with at most 128 offered targets.

Only local `file:` references are accepted. **Clipboard file imports follow
ordinary symlinks by default**, including links in parent folders. No folder
approval or extra prompt is required. Add `--no-host-clipboard-symlinks` to reject
linked sources for this and future workspace TUI clients instead. That strict
choice persists until `--host-clipboard-symlinks` saves link following again.
Both require an effective enabled clipboard preference. This changes clipboard
imports only: normal pathname/file drops and live sharing still refuse symlinks.

The selected alias and its actual target are checked against protected-path
rules. Kernel magic links, runtime/device paths, hard links, special/protected
files, directories and unsupported extensions remain refused. Both the selected
and target filename extensions must match the captured content. The target is
pinned without reading source bytes, then matched by identity to the existing
strict host-drop capture before any file bytes are read. Unsupported kernel
resolution contracts refuse rather than fall back to ordinary link following.

**A link may change between file-manager Copy and Ctrl+Y, so the file imported
may be the new target.** There is no folder allowlist or remembered target.
Once pinned, retargeting the symlink cannot redirect that read, but the file's
contents remain mutable; existing capture-time mutation checks still apply.
Up to four files may be imported, at most 8 MiB each and 16 MiB per batch, within
existing per-client quotas. A batch is validated before staging. Import always
snapshots copies, even for a cut selection: it never moves/deletes the source,
clears the clipboard or acknowledges a file-manager move. No remote URL fetch is
performed.

These MIME targets describe data, not proof it came from a file manager. The
shortcut authorizes their local file references without a per-file confirmation;
do not use it to discuss untrusted copied file URLs. Clipboard selection may
change between type discovery and payload retrieval; no atomic desktop snapshot
is promised. For independent per-image host confirmation, use `attach --broker`.

Arbitrary `text/plain`, bare text paths and raw PDF clipboard payloads are not
requested or reinterpreted. Copy a PDF **file** in a file manager; selected text
inside a PDF viewer remains ordinary text paste. PNG pixel data is still accepted
when no supported copied-file list is offered. Unsupported/missing content
produces a refusal placeholder with a fixed stage/category, without clipboard
data, source paths, raw exceptions or helper stderr. Source-guard failures remain
uniform for missing, inaccessible or protected sources and links refused by
strict mode.

An ordinary paste above the 16 KiB pathname-capture limit passes through
unchanged. While it is still framed as paste, it cannot trigger clipboard
reads or pathname imports. Only an exact, valid paste end lets interception
resume for later input. A recognized paste or ASCII CSI sequence with certain
embedded control bytes also passes unchanged without granting an import. The
relay resumes only after an unambiguous closing delimiter or final byte, and
a later Ctrl+Y press is required for a clipboard read. Repeated top-level
Escapes and common control keys after a pending Escape (including Enter, Tab,
and Ctrl+Y) pass through without disabling imports or reading the clipboard.
If Ctrl+Y does nothing after Escape, press an arrow key, then press Ctrl+Y
again in the same client. A complete recognized key or terminal report can
restore framing, but grants no import or host open itself. Recognized control
strings and canonical pastes reached this way stay opaque until their own end.
An overlapping OSC introducer in Kitty's reserved file-drop mode still stops
interception to protect OSC 72 packets. Ambiguous encoding, possible nested
frames and other untrusted boundaries still stop interception for that client.
An unfinished terminal reply or paste never grants a read.
On exit, `input-framing-recovered` reports that interception resumed;
`input-framing` or `input-incomplete` means it did not recover from a framing
problem. If needed, open a fresh native TUI client; the relay does not restart
OpenCode or the workspace. A framing refusal also names the parser category
without reporting raw input.

After an unexpected clipboard-read error, one later qualifying Ctrl+Y key
event may try again only if desktop helper reaping was verified. There is no
automatic retry or replay of the failed shortcut or paste, and a success does
not refill the per-client allowance.
Encodings without an event type cannot distinguish repeat from a new press.
A second unexpected read failure or unverified helper cleanup or staging
disables clipboard imports for that client. Unverified staging also stops
pathname and Kitty file imports in the same client and cancels pending Kitty
requests. Refusals use fixed categories; input and clipboard contents are not
logged.

No packages are installed automatically.
Missing helpers name the selected utility and its host package explicitly:
`wl-paste` is provided by `wl-clipboard`, and X11 uses `xclip`. These package
names are the same across the supported distro catalog; see the host test guide
for package-manager commands and a feature-branch checkout recipe.

Captured bytes use the existing private drop transfer and staging/cleanup
limits. The relay supplies a container pathname as native paste input; it never
synthesizes Enter or submits a prompt. Native attachment composition and sending
remain native behavior. `--image-preview` separately controls image visuals for
a qualifying owning launch.

An additional `start --host-clipboard` may attach to an existing private V2 owner
without restart, credential/configuration refresh or unlock. It saves the choice
for future clients and enables that new TUI client; existing clients are unchanged.
`--no-host-drop --host-clipboard` can
enable clipboard imports (including copied files) while retaining the explicit
drag/pathname-paste file-drop opt-out.
Explicit enable refuses V1. Web, terminal/maintenance, the manager/embedded
terminal, sync and informational commands ignore saved clipboard policy and
reject explicit clipboard startup flags. Do not reset a workspace just to enable
this feature.

```sh
oc-box --project ~/devel/demo --host-clipboard
# Later clients inherit that choice:
oc-box --project ~/devel/demo
# Save disabled without opening a client:
oc-box preferences --project ~/devel/demo --host-clipboard off
# For a new owning launch when image previews are also wanted:
oc-box --project ~/devel/demo --host-clipboard --image-preview
```

See the short [host test guide](host-clipboard-testing.md). Focused mocked tests
are not acceptance of KDE/Wayland/X11 behavior or native image composition.

### --no-host-clipboard

Save clipboard importing disabled for future workspace TUI clients and open
this client without the clipboard shortcut. Mutually exclusive with
`--host-clipboard`; neither flag live-toggles existing clients or changes the
independent host-drop preference. A saved strict symlink choice is retained.
To change only policy, use `oc-box preferences --host-clipboard off`.

### --host-clipboard-symlinks

Save ordinary symlink following for clipboard file imports for this and future
workspace native TUI clients. This is also the default when no symlink choice
is saved. Requires an effective enabled clipboard preference: a saved enable
is enough, or combine with `--host-clipboard`. Accepted only on `start` or
`reset`, and mutually exclusive with `--no-host-clipboard-symlinks`.

### --no-host-clipboard-symlinks

Save strict no-symlink clipboard sources for this and future workspace native
TUI clients. Requires effective clipboard enable (saved or explicit) on `start`
or `reset`. A symlink in the selected file or any parent folder produces the
same fixed source refusal as other denied sources. PNG pixel imports are
unchanged. An additional client can save/use the
choice without restarting the workspace owner; existing clients are not
live-toggled. Neither choice changes file-drop preferences or live sharing.
Use `preferences --host-clipboard-symlinks on|off` to save without opening a client,
even while clipboard importing is off.

```sh
oc-box --project ~/devel/demo --host-clipboard --no-host-clipboard-symlinks
# Saved clipboard enable and strict capture now apply without flags:
oc-box --project ~/devel/demo
oc-box preferences --project ~/devel/demo --host-clipboard-symlinks on
```

### --credential-mode env|selected|vault

With `credentials`, save an explicit forwarding mode for that workspace. `env`
retains legacy suffix/pass_env forwarding, optionally supplemented with selected
vault names. `selected` forwards only configured credential names, plus fixed
runtime/nonsecret routing settings. `vault` uses all current global entries on
each new V2 owner, not a frozen name list; nonempty per-name selections are
refused. Without a saved policy, V1 defaults to env and V2 to global vault-first.
Explicit env/selected policies remain overrides. V1 with saved vault mode uses
legacy env forwarding without all-global unlock. Changes apply on the next
owner, not to an active client. To replace an existing selection:

```sh
oc-box credentials --project ~/devel/demo --credential-mode vault --clear-credentials
```

### --attachments

Web only: explicitly select the visible, source-version-bounded browser File-drop
adapter described under `web`. Keeps the native app and its draft/Send controls
unchanged. No automatic prompt admission or host-path/URL reading.

### --workspace-layout

Web only: accepted for compatibility with earlier 1.8/1.9 commands. Studio
opens with or without this flag. The old compact session index is not served.

### --no-vault

V2-only, for one **owning** start (including `--import-session`), reset, or web
invocation. Skip vault use without changing the saved workspace policy. Global
mode temporarily uses legacy env-file forwarding; explicit env/selected modes
skip vault entries but retain their env constraints. The next normal owner uses
the saved/default policy again. Attached TUI/web clients reject this flag and
never unlock or refresh an existing service. V1, info/status/list/doctor,
credentials/secrets, terminal/maintenance, sync, stop, repair, purge, backup/info/
restore, upgrade, and other non-owning commands reject it. It does not erase
already persisted config/auth or hide secrets from existing container processes.

```sh
oc-box start --project ~/devel/demo --no-vault
oc-box reset --project ~/devel/demo --no-vault
oc-box web --project ~/devel/demo --no-vault --no-open
```

### --env-secret NAME

With `credentials`, append an uppercase environment credential name to the
workspace's explicit env selection, repeatable and deduplicated. In selected
mode it must be present/nonempty in the sourced host environment before launch;
a missing value aborts before unlock/setup. Env mode already forwards legacy
variables, so this list becomes restrictive only in selected mode. No value is
accepted here. Nonempty per-name selections are refused in vault mode; choose an
explicit env/selected mode to use them. Example:
`oc-box credentials --credential-mode selected --env-secret PERSONAL_GITHUB_TOKEN`.

### --vault-secret NAME

With `credentials`, select an existing encrypted vault entry name to unlock and
forward on normal owning start/reset/import/web launches. Repeat for multiple
names. The name is saved in workspace metadata, never its secret value. Vault presence is
checked when unlocking, not by this names-only configuration command. A name
cannot also be selected with --env-secret. Example:
`oc-box credentials --credential-mode selected --vault-secret BRAVE_API_KEY`.
Vault mode instead uses every current entry and refuses nonempty per-name lists.

### --clear-credentials

With `credentials`, clear both saved env-name and vault-name lists before adding
new selections on this invocation. It does not erase the vault, remove provider
auth/config, revoke credentials, or change mode unless --credential-mode is also
specified. Example: `oc-box credentials --credential-mode env --clear-credentials`
returns to the original env workflow without deleting encrypted vault entries.
Use `--credential-mode vault --clear-credentials` to replace selections with
dynamic global V2 use. Clearing alone does not change the mode.

### --output FILE

Backup-only. Required new encrypted backup output pathname. Its parent must
already exist outside the project/registry/private volume. Existing files or
symlinks are never overwritten, including a destination created during capture.
Example: `oc-box backup --project ~/devel/demo --output /backup/demo.ocbox`.
The archive suffix is conventional; the versioned format is checked on restore.

### --portable

Backup-only. Omit the container OS image while retaining project, complete
private home, sessions, registry evidence, and limits/credential-name settings.
The first normal launch after restore rebuilds the selected distro OS and
baseline tools. Custom system packages and native-cache compatibility are the
user's responsibility. Example:
`oc-box backup --project ~/devel/demo --portable --output /backup/demo.ocbox`.

### --recipient AGE_KEY

Backup-only, repeatable (up to 16). Encrypt the backup key envelope to a native
age X25519 public key, allowing backup without a password prompt. Any matching
private identity can decrypt it. Without this option age requests a backup
passphrase interactively. Public recipients are not secret; private identities
must be stored independently of the archive and project. Example:
`oc-box backup --recipient age1PUBLIC_KEY --output /backup/demo.ocbox`.

### --host

For `backup`, include the [host migration component](#include-host-settings-for-migration).
Without a workspace selector or `--portable`, back up only the host. For `restore`,
explicitly install the archived oc-box host settings and encrypted vault; omit
`--project` for a host-only restore from either type of host-containing archive.
A host-only archive cannot be used with `--project`.

### --opencode-auth-export FILE

For `backup --host`, include a native V2 credential export from a private file,
or use `-` for a binary stdin stream. The export is encrypted separately and is
never printed by `oc-box backup-info`.

### --include-opencode

For `restore --host`, additionally install the native OpenCode profile and saved
credentials when present. Requires separate typed confirmation and fresh
destination paths. It does not activate or start OpenCode.

### --identity FILE

Restore/backup-info only, repeatable. Read a native age X25519 private identity
file owned by the host user with private permissions, no symlink or extra hard
links. Plugins, SSH identities, and encrypted identity files are refused in the
initial format; passphrase-encrypted backups instead use age's terminal prompt.
No identity text or passphrase is accepted through argv/environment. Example:
`oc-box backup-info /backup/demo.ocbox --identity ~/.config/backup-age-key.txt`.

### --cpus NUMBER

Set the CPU quota in core units; fractions such as `2.5` are accepted by Podman.
Default: `4`. Set at creation or reset. Existing containers, even stopped ones,
require reset to change it; oc-box does not call `podman update`. Values are
validated by Podman. `0` requests no CPU quota where supported.
`sync` can save a new value when recreating a missing container for a registered workspace.

```sh
oc-box reset --project ~/devel/demo --cpus 8
```

### --memory SIZE

Set the memory limit using Podman size syntax, such as `512m`, `8g`, or `16g`.
Default: `8g`. Set at creation or reset; not a live update. The value is saved
for future launches. Limits require delegated cgroup controllers. Podman
validates the size and kernel support. `0` requests no memory limit where
supported. There is no automatic project/volume disk quota.
`sync` can save a new value when recreating a missing container for a registered workspace.

```sh
oc-box reset --project ~/devel/demo --memory 16g
```

### --pids-limit NUMBER

Set the container's process limit; default `2048`. oc-box requires at least
`32`. Set at creation or reset; changing an existing container requires reset.
`sync` can save a new value when recreating a missing container for a registered workspace.
Example: `oc-box reset --project ~/devel/demo --pids-limit 4096`.

### --device MAPPING

Add a persistent Podman mapping, repeatable. Supports
`/host/device[:/container/device][:rwm]` and CDI `vendor/class=name`.
New settings append to saved mappings; identical strings are deduplicated.
Changing existing mappings requires reset. Use `--clear-devices` to replace or
remove them. Host permissions, SELinux, and installed driver userland still
apply; device mapping alone does not install an NPU/GPU SDK.

```sh
oc-box reset --project ~/devel/demo --device /dev/accel/accel0
oc-box --project ~/devel/gpu --device /dev/dri/renderD128:rw
```

### --podman-arg=--OPTION[=VALUE]

Append one literal long-option token to the Podman create command before the
image. Repeat the flag for multiple options. Use `=` both after `--podman-arg`
and within valued Podman options; this is not a shell command string or a
space-split list. Values containing spaces remain one argument when quoted.
Saved options persist across launches/resets; identical strings are deduplicated.
Remove old/conflicting options with `--clear-podman-args` when replacing them.

The following are reserved by oc-box: `--name`, `--label`, `--label-file`,
`--cidfile`, `--replace`, `--rm`, `--entrypoint`, `--workdir`, `--user`,
`--rootfs`, `--pod`, and `--pod-id-file`. Use oc-box's dedicated CPU, memory,
and process flags instead of forwarding their Podman equivalents. Options are
for `podman create`, not global Podman flags or arguments after the image.

```sh
oc-box reset --project ~/devel/demo \
  --podman-arg=--shm-size=1g \
  --podman-arg=--ulimit=memlock=-1:-1
```

Extra devices, mounts, capabilities, host networking, and security options
extend the default boundary as requested by the host operator. Podman and the
host's rootless permissions determine which settings are available.

### --clear-devices

Clear all saved `--device` mappings before adding any new ones on this command.
Use during reset for an existing container. It does not remove a device passed
as a raw Podman option; use `--clear-podman-args` for that list.

```sh
oc-box reset --project ~/devel/demo --clear-devices
oc-box reset --project ~/devel/demo --clear-devices --device /dev/accel/accel1
```

### --clear-podman-args

Clear saved additional Podman options before appending replacements. Use during
reset for an existing container. It leaves the dedicated device list unchanged.

```sh
oc-box reset --project ~/devel/demo --clear-podman-args \
  --podman-arg=--shm-size=2g
```

### --delete-sessions

Purge-only. Delete recorded host archive session copies as well as environment
resources. Project files remain unless `--all` is used. It never deletes an
original host source conversation merely because that conversation was imported
into this workspace. Example: `oc-box purge --workspace WORKSPACE_ID --delete-sessions`.

### --all

Purge-only. Remove environment resources, recorded host session archives,
metadata, and the entire original project directory, including untracked files.
The directory identity and marker must still match. Example:
`oc-box purge --project ~/devel/disposable --all`.

### -y, --yes

Skip purge's ID-prefix confirmation. Purge-only; it does not answer OpenCode
questions or bypass permissions on other commands. Example:
`oc-box purge --workspace WORKSPACE_ID --all --yes`.

## Lifecycle and persistence

```text
Operation              Project   Private home/sessions   Container OS
Exit OpenCode          kept      kept; archive sync     kept; stopped
stop                   kept      kept                   kept; stopped
sync                   kept      kept; archive sync     kept; stopped
terminal (V1/maintenance) kept     kept; no archive sync  kept; prior run state
terminal (V2 attach)    kept      shared current session kept; owner controls stop
web (V2)               kept      shared current session kept; sync/stop on last client
attach (V2 direct)     kept      image message retained kept; existing owner only
attach (V2 broker)     kept      disclosed data retained kept; lease until broker exits
reset                  kept      kept, same session ID  rebuilt
purge                  kept      deleted                deleted
purge --delete-sessions kept      deleted                deleted
purge --all            deleted   deleted                deleted
```

Default purge retains host archive copies; `--delete-sessions` and `--all`
remove recorded host archive copies. Imported source host conversations remain.
Shared base images are retained by all operations. Stop/reset do not checkpoint
running processes. Packages installed in the private home survive reset;
system-wide packages do not. No command currently manages Btrfs snapshots.

CPU, RAM, process, distro, and mapping changes require reset for existing
containers. A stop/start alone reuses the old create-time settings. Avoid
changing resources directly through Podman if metadata consistency is required.
An active launcher holds a nonblocking workspace lock; sync, reset,
repair, maintenance terminals, and purge require that launcher to exit. V1
start also requires that launcher to exit. V2
TUI/terminal/web attach clients use same-user leases and can coexist without
refreshing credentials or taking the exclusive lock. Read-only list/status and
explicit stop can run from a second terminal. A crash releases the lock when
its process exits; an orphan still running can retain it.

## Distribution images

### Host dependency installation

Host requirements and workspace baseline packages are separate. These commands
cover core rootless tools and optional age backup tools; doctor prints only
packages missing for each feature. Run OS package commands manually with the
privileges appropriate to your machine (`sudo` on a configured regular host, or
as root without sudo in an administrative shell).

| Host distro | Core rootless prerequisites plus optional backup tools |
| --- | --- |
| Tumbleweed | `sudo zypper refresh && sudo zypper install python3 podman passt shadow catatonit conmon crun age` |
| Leap | `sudo zypper refresh && sudo zypper install python3 podman passt shadow catatonit conmon runc age` |
| Ubuntu / Debian | `sudo apt update && sudo apt install python3 podman passt uidmap catatonit conmon crun age` |
| Fedora | `sudo dnf install python3 podman passt shadow-utils catatonit conmon crun age` |
| Arch | `sudo pacman -Syu --needed python podman passt shadow catatonit conmon crun age` |
| Alpine | `sudo apk add python3 podman passt shadow-subids catatonit conmon crun age` |

Installing packages alone does not configure subordinate IDs, cgroups, or make
rootful Podman rootless. Alpine requires its release's community repository for
some providers; Ubuntu may require its official universe repository. Use a
current supported OS with Python 3.10+ and pasta-capable Podman; an older Leap/
Python combination may need an OS upgrade rather than a package reinstall.

| Feature | Host dependencies |
| --- | --- |
| Launch/reset/sync | Linux, running Python 3.10+, Podman, pasta (`passt`), newuidmap/newgidmap, catatonit, conmon, crun or runc, host OpenCode 1.x or 2.x |
| Maintenance/stop/repair/purge | Linux, Python, Podman and UID helpers; history preservation/deletion additionally uses host OpenCode |
| Full/portable backup | Linux, Python, rootless Podman/UID helpers, age/age-keygen (both from `age`), memfd support |
| Backup inspection | Linux, Python, age/age-keygen, memfd; no Podman/OpenCode/vault extra |
| Host-only restore | `age`, `age-keygen` and Linux anonymous-memory file support; no Podman |
| Workspace restore | Backup prerequisites plus pasta, init/helper/runtime providers for container creation |
| Optional vault | `cryptography` in the same Python environment as oc-box; no age requirement |
| GitHub installation/upgrade | Git and Python; one-line download also needs curl, mktemp and trusted CA certificates |
| Storage reports | GNU `du` from `coreutils`; BusyBox du is insufficient; other metrics still work if absent |
| FUSE overlay driver | `fuse-overlayfs` only if selected/needed; kernel overlay does not require it |
| Read manuals | man (`man`, `man-db`, or Alpine `mandoc`) and info (`info` or `texinfo`) |
| Regenerate manuals | GNU makeinfo (`texinfo`); contributor-only, never required to install/use oc-box |

Host OpenCode is not supplied by these mappings; see
https://opencode.ai/docs/#install. On a supporting launcher release, use
`oc-box upgrade --vault` for vault dependencies through the verified existing
owner; upgrade older channel-aware launchers normally first. First installations
can request the package `[vault]` extra or GitHub/source `--with-vault`.
Do not install Python dependencies into system Python to fix an isolated tool's
missing extra.

Inside each workspace, oc-box automatically installs Bash, CA certificates,
curl, Git, Python, ripgrep, tar/gzip, Node/npm, and PDF tools (`poppler-tools` on
openSUSE, `poppler-utils` on Debian/Ubuntu/Fedora/Alpine, `poppler` on Arch).
uv/uvx and matching OpenCode are bootstrapped inside private storage. MCPs,
formatters, language servers, plugins and project libraries may have their own
requirements; doctor does not infer those from project configuration.

Distro CI installs mapped packages in disposable instances of all seven catalog
images, checks actual command providers, tests standard age, and verifies scoped
guidance. It does not run nested rootless Podman or replace host lifecycle tests.

```text
Name         Image
tumbleweed   registry.opensuse.org/opensuse/tumbleweed:latest
leap         registry.opensuse.org/opensuse/leap:latest
ubuntu       docker.io/library/ubuntu:24.04
debian       docker.io/library/debian:stable
fedora       registry.fedoraproject.org/fedora:latest
arch         docker.io/library/archlinux:base
alpine       docker.io/library/alpine:latest
```

Aliases: `opensuse` and `opensuse-tumbleweed` select `tumbleweed`;
`opensuse-leap` selects `leap`; `archlinux` selects `arch`. `oc-box distros`
reports the actual compiled catalog. Images are pulled when missing, not
automatically refreshed to a newer remote tag on every launch.

## Sessions and recovery

The live database is private container storage. The normal host database is
not mounted. Normal exit and `sync` export root conversations using supported
mainline commands, then import namespaced host archive IDs with titles prefixed
`[oc-box project-name]`. Repeated sync refreshes the archive copy. Resume uses
the original container session, not that archive ID.

This is conversation-history sync, not live replication or a backup against
disk loss. Host archives omit snapshot stores, separate subagent transcripts,
running processes, installed packages, and files outside the exported transcript.
New work in a host archive is not merged back automatically. Container/home
storage normally survives a client crash or reboot; the last uncommitted turn
can still be lost. Retry missed archival with `oc-box sync`.

OpenCode 2.x history exports are checked for unsettled messages. A running direct
shell or compaction and an unfinished assistant message at the end of a session
are refused with a fixed reason
(`OpenCode 2.x history is unsettled; retry sync after completion.`); that session
keeps its last good archives, every other session is still synchronized, and
`sync` returns nonzero with the number of pending sessions. Retry after the work
finishes. An unfinished assistant message that the session has already moved past
(settled messages follow it in an idle session) never settles on its own, and
native OpenCode omits it from its export. This is typical of history migrated
from OpenCode 1.x after an interrupted or killed turn; 2.x itself settles an
older unfinished assistant when the next turn starts. The superseded-assistant
exception also refuses a session that is currently active. An inactive session
is archived as that native export; the archive annotation records the omitted
count and `sync` prints a notice with the number of affected sessions, never
their titles, IDs or content. Other failures show a fixed explanation when
the private history bridge can name one, otherwise only `podman failed (exit N)`;
native error text and private data are never printed.

For `--import-session`, quit the source client first. Its project must match
the selected directory. A private source export is saved in
`imports/SESSION_ID.json` under the workspace registry. oc-box imports into the
container, verifies every message/part, then launches that exact ID. Mainline's
removal of obsolete nonpending tool `state.raw` buffers is accepted, but tool
inputs/outputs, pending raw buffers, text, IDs, and metadata remain checked.

Do not routinely repeat the import flag. An already verified ID is not merged
or overwritten. If verification was interrupted/rejected, retry the same import
after updating the launcher: an existing session may be recovered by comparison
with the original saved export, without reimporting over container work.
Differences outside the known normalization stop launch and retain both copies.

```sh
oc-box --project ~/devel/demo --import-session ses_HOST_SESSION_ID
oc-box --project ~/devel/demo
oc-box sync --project ~/devel/demo
```

The source-tree diagnostic prints counts and differing field names, not text:

```sh
python3 tools/diagnose_import.py --workspace WORKSPACE_ID --session SESSION_ID
```

### Recover history after replacing a workspace marker

Older launchers allowed removing `.oc-box-id` and starting again to create a
fresh environment. Current launchers detect the registered path and refuse to
create a duplicate; run `repair` to reconnect the existing environment instead.
Removing the marker does not delete the old private volume, registry, or host
archives. If an older launcher already created a replacement, to continue a
previously synced conversation in the new environment, exit its active client,
find the host archive title prefixed `[oc-box project-name]`, and import its ID:

```sh
opencode --pure session list
oc-box --project ~/devel/demo --import-session ses_ARCHIVE_ID
```

Mainline records host archive imports against oc-box's private `controller`
directory. oc-box verifies the archive's namespaced ID against that workspace,
its original host registry, and its saved `history/SESSION_ID.json` before
restoring the project directory for transfer. Both saved records must identify
the selected project. The current host transcript is copied, retaining any
later host archive work; the host session and saved archive are not changed.
The archive title alone cannot authorize a different project.

After a plain purge with a current launcher, the same checks use its private
`retired/ID/` recovery records if the original active registry is gone. Older
purges that removed those records cannot be reconstructed automatically: use a
retained backup or a newer archive from the live workspace. Existing container
sessions remain usable without the retired registry; that registry is needed
only for verifying a host archive's old controller-directory origin.

Use the import flag once, then resume normally. If the original registry or
saved export is missing, restore it before retrying. Host history contains only
successfully synced turns; the old private volume may contain newer work. Keep
the original workspace resources until recovery is complete. Importing history
does not copy installed packages, snapshots, or other private-home files.

### Retire a stale duplicate without deleting the current project

Inspect the marker association and recorded sessions before cleanup:

```sh
oc-box list
oc-box --status
oc-box purge --workspace STALE_WORKSPACE_ID
```

Keep the workspace selected by the current project marker. Plain purge final-syncs
the stale workspace's history and removes only its owned container/volume/network
and active registry. It leaves the replacement workspace, project files, current
marker, and host conversation archives intact. If the final sync or recovery-copy
write fails, cleanup stops before removing resources. Do not use `--all` or
`--delete-sessions` for this history-preserving duplicate cleanup. oc-box never
chooses and deletes duplicates automatically. If the old workspace has newer
unsynced work, recover it before retiring its private home.

## Devices and host permissions

The host user must be able to access the device. Host supplementary groups are
not automatically preserved; where group access is required, use installed
crun and `--group-add=keep-groups`. A same-named group inside the container is
not the host group. Newly acquired groups require a fresh host login/process
with those groups. Driver-specific SDKs must be installed inside the container.

Before creating/recreating device-enabled containers, oc-box checks requested
host read/write access and SELinux's device boolean, including `/usr/sbin`
utilities that may be absent from PATH. Warnings are advisory; oc-box does not
run sudo or change host policy. The policy warning is skipped when device access
is already allowed, SELinux is not enforcing, or the container explicitly
bypasses labeling. CDI devices receive the SELinux check, not a host-path check.

```sh
oc-box reset --project ~/devel/npu \
  --device /dev/accel/accel0 \
  --podman-arg=--runtime=crun \
  --podman-arg=--group-add=keep-groups
```

SELinux can deny access even when a mapped device name is visible with
`-?????????` attributes. From the host, inspect policy and permissions:

```sh
ls -ldZ /dev/accel
ls -lZ /dev/accel/accel0
id
/usr/sbin/getenforce
/usr/sbin/getsebool container_use_devices
test -r /dev/accel/accel0 && test -w /dev/accel/accel0
```

If the user has access and enforcing SELinux has the device boolean off,
Podman's documented host-wide policy setting is:

```sh
sudo /usr/sbin/setsebool -P container_use_devices on
```

It permits device labels subject to mappings and Unix permissions; it does not
map extra devices or disable SELinux. It persists and applies immediately,
including to running containers, without reset/reboot. A memlock problem does
not explain failure to read device attributes. Once access works, inspect the
driver and limits independently inside the container:

```sh
ls -l /dev/accel/accel0
prlimit --pid $$ --memlock
xrt-smi examine
```

Requested memlock limits are still subject to the host user's hard limit and
kernel/runtime rules; passing a ulimit does not grant host privileges.

## Resource reports

Status collects running state, CPU, RAM/limit, PIDs, uptime, configured limits,
image, storage driver, restart/exit/OOM/health information when available, and
the latest saved session and archive IDs. Device mappings and saved raw Podman
options are included. No container commands run during collection.

It also checks the project's regular marker and recorded filesystem identity,
identifies
multiple registered workspaces for the same project in an all-workspace report,
and includes specific repair or history-preserving cleanup instructions.
JSON fields `project_marker_workspace` and `related_workspace_ids` expose those
associations. Suspicious/missing projects are not scanned for storage usage.
`Host archive: not recorded` means this workspace has not recorded a successful
sync of its selected session yet; the live volume/session remains intact. Exit
normally or, after the launcher exits, run `sync` to create the host archive.

Storage is split into writable OS layer, private home volume, and project.
The rootfs size including shared image layers is separate. Filesystem used/free
space and block-I/O counters are shown. CPU is a short sample where 100% is one
core; stopped containers have no live sample. Missing/unavailable values are
not reported as zero. Rootless pasta network counters are omitted as unreliable.

Directory scans count allocated blocks without following symlinks or crossing
filesystems, with a 15-second timeout per directory. Private-volume scans use
`podman unshare` on the host, not container exec. Very large caches may take time
or report unavailable size. Shared image layers must not be summed as unique
storage. Btrfs compression, reflinks, snapshots, and shared extents make these
estimates different from exclusive physical usage. There is no disk quota.

```sh
oc-box --status
oc-box --status --workspace WORKSPACE_ID --json > workspace-status.json
```

## Configuration and environment

On launch, global OpenCode configuration, agents, skills, local plugins, and
MCP settings are copied into private storage; they are not host mounts.
Project configuration runs natively inside the container. Removed host plugins
are removed from the copy. Container dependency caches and generated SDK locks
are reused when host dependency inputs and runtime version are unchanged.
Cold plugin resolution has its own timed setup stage before the TUI opens.

The generated profile selects Bash and enables native formatting when not
configured, plus native LSP on 1.x only. The V2 container-only Build helper
auto-allows final tool permission asks, preserving denies passed to the hook.
The existing final Build wildcard still overrides copied ordinary global/Build
tool permission rules, including their configured denies, as before.
Plan/subagent and plain host permissions are unchanged. V1 behavior is unchanged. Other copied
plugins may have independent checks. Changes to host config are copied at the
next owning launch; native V2 reload inside the container does not fetch new host
config or vault values. Host-only executables must be installed inside the
container. Native OpenCode saved/OAuth credential refresh and copying remain
independent in every credential mode, including global vault mode; refreshed
container auth is not written back to host auth. Global vault entries are
environment variables, not replacements for structured OAuth refresh-token
records. Existing copied/container config/auth secrets remain independent;
changing credential mode does not revoke them.

The new V2 helper makes only a **final `ask` to `allow`** conversion for the
resolved actual Build agent; it never overwrites a deny passed to it. An explicit
permission event agent wins; when omitted, resolve the current session's agent,
then the native configured
default, never the parent's agent. Explicit session and mandatory policy denials
remain denied, as do denials from managed rules that apply after the private
overlay. Unknown-agent requests retain native ask/deny outcomes and are never
treated as Build. This does not preserve ordinary copied Build/global denies
overridden by the existing Build wildcard. All non-Build rules remain intact.
A Build parent does not grant Build permission treatment to Plan/child requests.
Session permission arrays are
not overwritten, so Build allowances do not leak into Plan or child rules.
Question and MCP elicitation prompts are separate from permission asks and may
still need user input.

The final private `OPENCODE_CONFIG_CONTENT` overlay reenables only the generated
Build helper against lower-priority project plugin removal entries; managed
policy still wins. Helper activation relies on this overlay, not on an external
agent transform running after every config source. No `--auto` or client-wide
autoaccept flag is added to oc-box clients. The new native evaluator/import/reload
fixture targets these boundaries offline, not live host acceptance.

The container Build-only allow-all default is not native V2 client-wide
autoaccept: Plan/subagent configured rules and plain host permissions remain
unchanged. As an optional one-off **host OpenCode** choice,
`opencode --auto --standalone` auto-approves permission asks, not explicit
denials, and uses normal host env/auth rather than the oc-box vault. In a running
native TUI, Ctrl+P → Open settings → Sessions → Permissions → autoaccept is the
corresponding user setting (`cli.json` `session.permissions`). That broader
preference can persist in the client's global CLI settings; oc-box does not
enable it or edit normal host `cli.json`. It is not a blanket replacement for
the Build-only default. Inside oc-box, the saved CLI file belongs to that private
container home and is retained across V2 config refreshes. The separate scripted
global-vault host fixture has passed native tool/permission and reload checks.
A new interactive TUI permission-prompt/reload pass is still pending; the scripted result is not
interactive UI acceptance or security certification.

HTTP endpoint keys in global config using localhost, 127.0.0.1, or ::1 are
rewritten to `host.containers.internal`. OAuth redirect URIs and shell commands
are not rewritten. Container localhost remains local to the container.

### Environment variables

`OC_BOX_STATE_HOME` overrides the host workspace registry root. Otherwise it is
`$XDG_STATE_HOME/oc-box`, defaulting to `~/.local/state/oc-box`. Use the same
registry when looking up existing workspace IDs; changing it does not migrate
old resources automatically.

`XDG_CONFIG_HOME` selects the source global OpenCode/oc-box configuration;
default `~/.config`. `XDG_DATA_HOME` selects the normal host OpenCode data root;
default `~/.local/share`. The installer itself uses fixed `~/.local` paths.
Container XDG paths use the private home, not host XDG directories.

Without an explicit workspace policy, normal V2 owning launches use all current
host vault entries. A present vault replaces ambient automatic `_API_KEY`/
`_TOKEN` and `pass_env` forwarding, including legacy `OPENCODE_AUTH_CONTENT`
overrides; no vault present retains env-file fallback with setup guidance.
Ordinary native OpenCode saved/OAuth credential refresh/copy remains independent
in every mode. V1 and explicit `env` policy
retain the legacy rules below; `selected` policy instead uses only selected
credential names. `--no-vault` is a one-owning-invocation V2 compatibility opt-out,
not a saved default or a way to refresh an attached service.

Under legacy env forwarding, variables ending `_API_KEY` or `_TOKEN`, plus
`GH_TOKEN` and `GITHUB_TOKEN`, are passed when set. Terminal color/language
settings and `LEMONADE_BASE_URL` remain noncredential runtime preferences. These
forwarding rules apply to normal OpenCode launches, not maintenance terminals.
For an intentional legacy env override, add other uppercase names (not values)
in host oc-box configuration:

```json
{
  "pass_env": ["MY_MCP_SETTING", "MY_PROVIDER_SECRET"]
}
```

Save that as `~/.config/oc-box/config.json` (or its XDG equivalent). Only values
present in the host environment are copied. Forwarded HTTP localhost URLs are
mapped to the host-service name. The default `LEMONADE_BASE_URL` is
`http://host.containers.internal:13305/api/v1`. `TERM` defaults to
`xterm-256color`. Host PATH, shell startup scripts, proxy variables, engine
sockets, and SSH agents are not forwarded by default.

Separate password creation minimums can be saved in the same JSON object:

```json
{
  "vault_min_passphrase_length": 12,
  "backup_min_passphrase_length": 16
}
```

Each defaults to 8 and accepts an integer from 8 to 1024. They set a minimum
character count, not an exact size; all passphrases remain bounded to 1024 UTF-8
bytes. `--min-passphrase-length N` overrides the relevant setting for one
creation/change. Invalid policy fails before vault unlock or backup lifecycle
changes. Existing passwords and recipient-encrypted backups do not consult
these creation settings. Preserve your other keys when editing this file.

The same JSON object can include `"auto_update": true` to opt in to anonymous
GitHub checks/installation before normal installed launches, or use `oc-box
upgrade --auto-update on`. The setting is off by default and is not forwarded
into the container. `OC_BOX_UPDATE_CHECKED=1` is an internal one-launch guard
used when restarting the host launcher after automatic installation; it prevents
recursion, not authentication or a workspace identity override.

Forwarded credential/custom environment values are passed through the Podman
client's environment, not embedded in its process arguments. They are still
accessible to container processes and same-user environment inspection. Copied
configuration and auth can also contain secrets; only supply credentials the
agent should use. See SECURITY.md for the documented trust boundary.

### Native V2 provider and MCP environment references

Store values with `oc-box secrets set ANTHROPIC_API_KEY` and
`oc-box secrets set MCP_API_KEY` before the first V2 owning launch. OpenCode
configuration contains **references only**, not secret values. Merge the fields
you need into global `~/.config/opencode/opencode.json(c)` or project
`opencode.json(c)`, preserving unrelated settings. The published V2 shapes use
`providers.PROVIDER.settings` and `mcp.servers.NAME.environment`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "providers": {
    "anthropic": {
      "settings": {
        "apiKey": "{env:ANTHROPIC_API_KEY}"
      }
    }
  },
  "mcp": {
    "servers": {
      "example-tools": {
        "type": "local",
        "command": ["example-mcp-server"],
        "environment": {
          "MCP_API_KEY": "{env:MCP_API_KEY}"
        }
      }
    }
  }
}
```

Replace the placeholder MCP executable with one installed in the container.
Remote API-key-only MCP servers can use `mcp.servers.NAME.headers` with
`"Authorization": "Bearer {env:MCP_API_KEY}"` and `"oauth": false`; prefer native
OAuth sign-in when supported. JSON shell expressions such as `$MCP_API_KEY` are
not environment references. Providers/MCPs resolve the owning service's
environment, and session shells/tools inherit delivered entries. New vault
values or names require a new owner; attach and native config reload do not
replace its environment. See the published V2 config and MCP guides at
https://opencode.ai/docs/config and https://opencode.ai/docs/mcp-servers.

Plain `opencode --standalone` is a one-off **host OpenCode** command using normal
host environment/auth, not an oc-box command and not an oc-box vault consumer.
For one-off env-file compatibility only:

```sh
. ~/.config/provider-credentials.env
opencode --standalone
# Or one owning V2 oc-box launch without vault delivery:
oc-box --project ~/devel/demo --no-vault
```

The recommended primary V2 oc-box workflow is global vault-first, without
sourcing a file or registering project selections. Native OpenCode saved/OAuth
credential refresh and copying remain independent in every mode, including
global vault mode. oc-box never imports or rewrites plaintext OpenCode JSON
secrets into its vault.

Legacy env forwarding can use `OPENCODE_AUTH_CONTENT` as a provider-auth
override; configured global vault mode suppresses this ambient override, without
disabling ordinary native OpenCode saved/OAuth credential refresh and copying.
The launcher sets `OC_BOX_CONTAINER=1`, `OPENCODE_DISABLE_SANDBOX=1`, and
`OPENCODE_DISABLE_AUTOUPDATE=1` inside it, plus its private profile/config.
OpenCode's existing Bubblewrap plugin is disabled only for this launch;
Podman is the environment boundary. No OpenCode fork is installed.

## Files

### Host installation

`~/.local/bin/oc-box` is the launcher. `~/.local/share/oc-box/` contains installed
Python sources, documentation, LICENSE, NOTICE, and embedded `build-info.json`.
New installations stage complete versioned copies under `releases/` and use a
`current` symlink plus compatibility pointers for the documented top-level files.
The launcher pins a release path so other active launchers keep their code
unchanged. Old copies are retained, not automatically deleted. When first
switching from an older flat installation, exit old active launchers before
reinstalling; only launchers installed with release-path pinning have this
concurrent-update guarantee. The source checkout and installation are
independent; use `upgrade` or reinstall after updates.
`~/.local/share/man/man1/oc-box.1` and `~/.local/share/info/oc-box.info` are the
installed manuals. Installation requires no man/Info build tools.

Optional GitHub TUI dependencies are private to each immutable release; they do
not replace system Python packages. See Installation and manuals for opt-in
flags and the Python-package extra.

### Host registry and archives

`~/.local/state/oc-box/workspaces/ID/workspace.json` holds directory identity,
container/volume names, distro, limits, and saved Podman/device settings. Explicit
credential policy contains mode and selected names only, never values. Global
vault mode does not save an automatic entry-name list.
`project_identity` retains the last verified device/inode for diagnostics;
`project_filesystem_identity`, when supported, records `linux-file-handle-v1`
filesystem ID and opaque persistent directory handle. Driver-provided handles
can include generation, dataset/subvolume, or other identifying information;
oc-box neither decodes them nor selects a format by filesystem name. The
transient mount ID is not recorded. The registry need not share the project's
filesystem: no hard links or filesystem-specific utilities are used.
`sessions.json` (1.x) and `sessions-v2.json` (2.x) hold separate session IDs and
archive associations. `imports/` and `imports-v2/` hold
private source exports; `history/` holds private host archive JSON files.
V2 resume state also records missing source IDs without removing their saved
archive associations. Archive relationship annotations remain in `history/` and
host transcript metadata, including private provenance retained by plain purge.
`lock` serializes mutating commands. These files are outside the project and
not mounted into the container. Transcript files may contain private content.

`.oc-box-id` in the project associates the directory with its registry. Do not
edit or copy it to authorize a different mount. Identity checks include the
canonical path, inode, and supported filesystem ID/persistent handle; older or
unsupported filesystems use the conservative device/inode pair. A verified
device-only change does not by itself invalidate a stable directory baseline.
Deleting/recreating the same path or losing its marker does not silently select the old environment or create a
duplicate while an active registration remains. An explicitly requested
`repair` can accept the current identity at the same path and restore a missing
marker, retaining the existing environment. With several registrations, select
the intended workspace by ID; never copy markers between unrelated directories.

Plain purge retains verified archive recovery evidence under
`~/.local/state/oc-box/retired/ID/`, outside the active workspace registry. It
contains only minimal ID/project provenance and private `history/` JSON files,
not container storage, auth, installed dependencies, or the full live database.
Keep it while those host archives may need importing; it is private saved data,
not a project file or active environment. It has no automatic expiry.

The normal host OpenCode database, usually under `~/.local/share/opencode/`,
contains archive copies. Old installations may retain one-time relocation
backups under `~/.local/state/oc-box/migrations/`; these are private data, not
workspace resources or release artifacts, and later workspace purge leaves
them intact. The original development-only relocation helper is retired.

### Dashboard state and activity journal

The experimental dashboard keeps favorites as private UI state, not native
OpenCode pins. The oc-box activity journal is `log/activity.jsonl` under
`OC_BOX_STATE_HOME`, or `$XDG_STATE_HOME/oc-box` (default
`~/.local/state/oc-box/log/activity.jsonl`). By default it rotates to
`activity.jsonl.1` through `.3`, with four 256 KiB files. Files use mode 0600
and log directories mode 0700. Readers do not create files. `oc-box logs --path`
shows the active private pathname. `oc-box logs` and the dashboard's oc-box
source share the same validated severity and message formatting. Only the CLI
defaults to `info` or higher; the dashboard includes `debug`. Retained valid
older entries without a severity remain readable with a derived level.

Generated entries use fixed categories and allowlisted metadata, such as a
workspace ID, stage, duration, or aggregate count. Reason codes and metric
names come from fixed lists, and metric values are bounded integers. Entries
do not record raw arguments, environment values, errors, stdout/stderr,
prompts, transcripts, pairing links, authentication or clipboard data, or
other free-form text.
This is bounded activity metadata, not a terminal recording or a permanent
audit archive. The dashboard's separate opt-in Podman log view stays in memory
and never adds content to the journal; its best-effort credential redaction
cannot guarantee that arbitrary secrets are absent.

**Private log location and rotation.** Add a `logs` object to the existing host
configuration, `~/.config/oc-box/config.json` or `$XDG_CONFIG_HOME/oc-box/config.json`.
Keep the other keys. For example:

```json
{
  "logs": {
    "directory": "~/.local/state/oc-box-logs",
    "journald": false,
    "SystemMaxFileSize": "512K",
    "SystemMaxFiles": 4,
    "SystemMaxUse": "2M",
    "SystemKeepFree": "1G",
    "MaxFileSec": "1d",
    "MaxRetentionSec": "14d"
  }
}
```

Use `--log-dir PATH` to override only `logs.directory` for one lifecycle
invocation or private `oc-box logs` read. A custom directory must be absolute
after `~` expansion. oc-box can create it privately on append; it won't fix
unsafe permissions or follow symlinks. Use a dedicated directory: oc-box
refuses to append to an unrecognized active file or rotate or prune an
unrecognized archive. Don't precreate `activity.jsonl`. Custom rotations
never overwrite an occupied destination. An interrupted or unverified prune
can leave a reserved `.activity.prune` file that blocks later custom writes;
it is retained for inspection, not automatically deleted. These checks do not
isolate files from other processes running as your user. A dashboard save
checks an existing custom directory without creating it. Changing the directory
does not move or delete the previous journal, alter rotation settings, or
change which workspace registry is used.

| Private setting | Meaning | Default |
| --- | --- | --- |
| `SystemMaxFileSize` | Size that triggers rotation, 1 KiB–4 MiB | `256K` |
| `SystemMaxFiles` | Active file plus rotations, 1–16 | `4` |
| `SystemMaxUse` | Total retained journal bytes, up to 16 MiB | File size × count, capped at 16 MiB |
| `SystemKeepFree` | Filesystem free-space reserve, in bytes; zero disables it | `0` |
| `MaxFileSec` | Age limit for the active file; zero disables it | `0` |
| `MaxRetentionSec` | Age limit for retained journal files; zero disables it | `0` |

Size settings accept bytes or a single binary `K`, `M`, or `G` suffix. Age
settings accept seconds or a single `s`, `m`, `h`, `d`, or `w` suffix, up to one
year. Rotation and expiry are checked on append; there is no timer, and a
reader never prunes files. Age cleanup applies to whole files, not individual
entries. Only the private journal's own validated files are eligible. If the
requested free-space reserve cannot be met, an append may be skipped. Explicit
flags override only valid saved policy, without changing host configuration or
the environment. Invalid saved `logs` policy fails closed even when a flag is
present: private logging does not silently select a different journal, and a
failed append does not change lifecycle results.
These settings borrow journald's names; **they do not edit `journald.conf` or
change the system journal**.

**Optional system journal mirror.** The host `logs.journald` setting is a
JSON boolean and defaults to `false`. Set it to `true` to enable mirroring by
default, or use `--journald` or `--no-journald` on a lifecycle command to choose
for one invocation. oc-box mirrors only validated activity events after they
have been successfully appended to the private file. The file remains the
primary journal. Mirroring uses native journal priorities and never includes
raw output, authentication data or secrets. A missing sink or delivery error
cannot change lifecycle results. No daemon is started, installed or
reconfigured; an ordinary installation does not require systemd.

The system journal can be visible to administrators and other authorized
journal viewers. It follows system retention and priority policy, which may
discard `debug` entries, so it need not contain the same history as the file.
`oc-box logs` still defaults to the private file regardless of the writer
setting. On the host, read the mirror explicitly with `oc-box logs --journald`,
or query the matching records directly:

```sh
oc-box start --project ~/devel/demo --journald
oc-box logs --journald
journalctl SYSLOG_IDENTIFIER=oc-box "_UID=$(id -u)" OC_BOX_FORMAT=oc-box-activity-v1
```

### Container data

The home volume is `oc-box-home-ID`, mounted at the host user's home pathname.
OpenCode data lives under its `.local/share/opencode/`. Copied configuration and
generated `oc-box-profile.json` live under `.config/opencode/`. The matching
OpenCode executable is `/opt/oc-box/runtime/bin/opencode`; Node/npm, Python/uv,
Bash, Git, ripgrep, curl, archive tools, and PDF tools are provided by baseline
setup. Additional system tools belong to the container OS layer.

## Exit status

`0` means successful informational/lifecycle work or normal dashboard exit.
A native OpenCode conversation TUI launch normally returns OpenCode/Podman's
exit code. oc-box validation, backend, setup, and sync failures return `1`;
a successful native TUI followed by failed history
sync also returns `1` and retains container data. Argument syntax errors return
`2`. A Python KeyboardInterrupt handled by the launcher returns `130`.
An exception during final cleanup can also cause a nonzero result.
For owning V2 TUI/web launches, a live-readiness timeout alone does not force
failure if the raw client exited `0` and guarded post-stop archival, native
verification, recovery cleanup and final stop all complete. The interruption and
unconfirmed-background-completion warning remains. Raw client failures and
cancellation remain nonzero even if archives were saved; unknown client status,
pending/partial history or failed/uncertain cleanup cannot become success.
Failed sync, recovery or cleanup retains the guidance to retry `oc-box sync`.

Warnings about host devices/SELinux do not themselves make creation fail.
Podman/driver errors still propagate. A nonzero status is not permission to
purge data; recover or inspect the existing environment first.

## Examples

### Everyday start and resume

```sh
mkdir -p ~/devel/demo
oc-box --project ~/devel/demo
oc-box --project ~/devel/demo
oc-box --project ~/devel/demo --new
```

The second launch resumes work. A new conversation does not reset files or
installed tools. Exit OpenCode normally to synchronize history and stop it.

### Inspect from a second terminal

```sh
oc-box list
oc-box --status
oc-box logs --project ~/devel/demo --level warn
oc-box --status --project ~/devel/demo --json > status.json
podman ps -a --filter label=io.oc-box.managed=true
```

### Grow an existing environment without losing progress

Exit its client before reset. Preserve source, private home, and session IDs,
but reinstall project-specific system packages afterward:

```sh
oc-box reset --project ~/devel/demo --cpus 8 --memory 16g --pids-limit 4096
```

### Continue a host session and recover after a crash

```sh
oc-box --project ~/devel/demo --import-session ses_HOST_SESSION_ID
oc-box --project ~/devel/demo
oc-box sync --project ~/devel/demo
```

Import once; normal launches use the saved container session afterward.

### Replace persistent hardware settings

```sh
oc-box reset --project ~/devel/npu --clear-devices --device /dev/accel/accel0 \
  --clear-podman-args --podman-arg=--runtime=crun \
  --podman-arg=--group-add=keep-groups
```

### Remove a disposable project completely

```sh
oc-box purge --project ~/devel/disposable --all
oc-box list
```

If the directory was deleted manually, use its workspace ID instead:

```sh
oc-box list
oc-box purge --workspace WORKSPACE_ID --all
```

The last command deletes its remaining managed resources and recorded host
archives; an unrelated replacement directory will not be deleted.

## Troubleshooting

### Launcher diagnostics

Use `oc-box logs --verbose=ALL` to inspect retained activity without restarting
a workspace. For diagnostics from the current invocation:

```sh
oc-box start --project ~/devel/demo --verbose=ALL
oc-box sync --project ~/devel/demo --verbose=warn
```

Use `oc-box logs --tail --follow` in a second terminal to watch preparation
tasks and their outcomes. The launch form `--verbose=ALL` also shows live
installer streams; the `logs` form does not change a running launcher.
From a source checkout, run `python3 tools/demo_preparation_logs.py` to see
both modes with synthetic subprocesses and a disposable private journal, with
no Podman, real workspaces or OpenCode sessions involved.

Scripts that consume older verbose lifecycle messages should continue using
`--lifecycle-diagnostics` for routine start/done lines in a pipe. Neither flag
enables native debug logging, adds raw child output to the journal or changes
lifecycle behavior. Native output and interactive prompts remain separate.

### Private output capture and uv Python

V2 startup copies native OpenCode saved/OAuth credentials independently of the
oc-box vault. `--no-vault` skips vault entries, not native authentication or the
private output capture that protects it. The final error after cleanup
explains a failed startup stage. Use the launcher diagnostic settings above
for additional safe stage details; they do not expose captured private output.

Some older uv-managed Python builds omit `os.memfd_create` even on capable Linux
hosts. oc-box first uses the Python binding, or, when it is absent, lazily calls
libc's `memfd_create` with close-on-exec enabled. Both paths use the same kernel
facility, never disk-backed temporary files or architecture-specific raw syscalls.
A denied/failed native call is not retried through libc. Capability checks happen
before container startup or reset deletion; preflight failures leave the existing
container and workspace intact.

```sh
oc-box doctor --feature private-output
```

If the capability is still unavailable, the error identifies the Python running
oc-box. For a `uv tool install` installation, compare it with system Python:

```sh
"$(uv tool dir)/oc-box/bin/python" -c 'import os,sys; print(sys.executable); print(sys.version); print("memfd_create:", hasattr(os, "memfd_create"))'
/usr/bin/python3 -c 'import os,sys; print(sys.version); print("memfd_create:", hasattr(os, "memfd_create"))'
```

An absent Python attribute alone is no longer a failure if the libc path works.
If system Python is 3.10+ and supports private capture, recreate only uv's tool
environment with it, retaining any extras originally installed:

```sh
uv tool install --force --python /usr/bin/python3 oc-box
```

This does not install into system Python's site-packages. Retry the original
launch; no reset, purge, vault setup, or workspace-image change is needed. A kernel
or security-policy denial still fails closed; switching interpreters cannot
bypass such restrictions. Backup/restore uses the same capability helper.

For a disposable host validation of V2 global-vault first-launch delivery,
Build approvals/reload and full backup/restore, use the source-checkout fixture:

```sh
uv run --isolated --no-project --with cryptography python -I -B tools/test_global_vault_podman.py \
  --opencode-v2 "$(command -v opencode)" \
  --distro tumbleweed --work-dir /var/tmp --temp-dir /var/tmp
```

Run from the repository root in a regular host terminal, outside oc-box and
other shell sandboxes, without sudo. This creates fresh private synthetic
vault/history/projects and verified separate Podman graphroot/runroot state;
it does not install a launcher or use normal host credentials/services. Choose
existing disk-backed work/temp parents with enough space/quota. TUI/prompt input
is scripted; this is not interactive TUI or comprehensive security proof. The
fixture retains its report, resources and encrypted evidence. Use only its
printed label/store-validated cleanup command, never global prune or manual
marker deletion. See `docs/opencode-2.md` for exact scope. No new live result is
accepted merely by preparing the fixture or passing its offline unit tests.

### Reboot, remount, and directory identity changes

Clearing `~/.cache` does not erase the oc-box registry (normally under
`~/.local/state/oc-box`) or private Podman volumes, and it does not change a
directory's identity. Linux device numbers can change across reboot/remount;
Btrfs commonly assigns a different `st_dev` while the directory inode stays
the same. Those mount-local numbers are not persistent disk identifiers.

The algorithm is capability-based through Linux's common VFS interfaces, not
Btrfs-specific. It uses no ext4, XFS, Btrfs, bcachefs, or ZFS-specific parser,
ioctl, tool, or device path. On filesystems exposing `name_to_handle_at`, oc-box
combines the directory's opaque persistent handle with the `statvfs` filesystem
ID and inode. Drivers supply their own handle encoding; different formats and
bounded variable sizes are accepted without a filesystem allowlist. If this stored identity
matches and only the device number changed, launch/list/status accept the same
directory automatically. No cache deletion, repair, reset, or session import is
needed. Normal lifecycle launch updates the last verified device/inode while
holding the workspace lock; read-only listings do not write metadata. No extra
command-line tools, mount operations, or elevated capability are required for
the handle lookup. The private registry and project may be on different
filesystems, and a project's bind-mount location need not be the filesystem root.

New workspaces record that baseline at creation. Matching old registrations
are upgraded during the next normal start/sync/reset/terminal, or by explicit
repair. If an old registry already differs after reboot and lacks the stored
baseline, run one explicit repair after checking the directory; the launcher
cannot safely infer the original filesystem from an old device number and
matching inode. Keeping the same inode on a different filesystem is not enough.

If persistent handles are unsupported (including some overlay/network mounts),
or the filesystem ID is unavailable/zero, the existing strict device/inode
check remains. Driver/kernel support, not the filesystem's name, determines
this behavior. oc-box does not claim zero-repair reboot compatibility with every
possible filesystem driver. Once a strong baseline is
recorded, an unavailable or different handle does not downgrade to a weaker
check: it requires explicit repair. Filesystem/subvolume/snapshot restoration,
inode/generation changes, copied markers, and mount replacements remain
recovery decisions for the host operator. Older diagnostics incorrectly treated
any changed device number as a replaced directory; do not remove the marker.

A filesystem-independent logical ID would be just the canonical path plus a
marker checked against the host registry. That works for selecting a workspace,
but it cannot distinguish an exact copy/restore of that path and marker from the
original directory. A signed marker has the same limitation because copies carry
the signature too. oc-box deliberately keeps physical replacement checks and
explicit repair when the OS cannot provide enough evidence, rather than silently
weakening the trust boundary on an unsupported driver. Clone/migration/restore
operations are explicit repair cases, not ordinary reboot cases.

Keep the marker to retain the association with the existing environment.
Update/reinstall the launcher, check that the project at the registered path
is correct, and exit its active client. Then run from a host terminal:

```sh
oc-box repair --project ~/devel/demo
oc-box --project ~/devel/demo --resume
```

If the container is still running, use `oc-box stop --workspace WORKSPACE_ID`
and retry repair. Repair preserves the container, private volume, and session
IDs. It does not recover storage that was deleted separately.

If the marker was already deleted, current launchers search the registry by
canonical project path and print existing IDs/session information instead of
creating a fresh environment. To restore a selected association:

```sh
oc-box --status
oc-box repair --workspace WORKSPACE_ID
oc-box --workspace WORKSPACE_ID --resume
```

If multiple records share the path, a path-only repair does not guess. Choose
the intended workspace by ID; another workspace's existing marker is not
overwritten. If a replacement is already in use, retain it and remove the
stale record with plain ID-selected purge as described in Sessions and recovery.

If the marker has no matching host registry, check `OC_BOX_STATE_HOME` and
`XDG_STATE_HOME` and restore the original registry before resuming. The default
registry is `~/.local/state/oc-box`, separate from `~/.cache`.

### Missing delegated CPU or memory controllers

On some openSUSE hosts a vendor user-manager drop-in clears delegation.
Startup then fails on `memory.swap.max` or missing cgroup controllers despite
kernel support. The launcher reports this before starting the container.
From a host terminal, substitute your numeric UID for 1000:

```sh
sudo systemctl edit user@1000.service --drop-in=90-oc-box-delegate.conf
```

Save the override:

```ini
[Service]
Delegate=cpu cpuset io memory pids
```

Run `sudo systemctl daemon-reload`. If the current user manager still lacks the
controllers, fully end/restart it through a fresh login or reboot. A reboot is
not a blanket Podman installation requirement. Check effective delegation in
`/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/cgroup.controllers`.
Retry the existing workspace without reset or purge. Do not edit vendor files.

### Project mount denied by SELinux

New mounts use Podman's shared `:z` project relabeling. Older mounts can exist
but deny file access. Relabel only the project from the host:

```sh
chcon -R -t container_file_t /path/to/project
```

Resume normally; no session reimport/reset is needed. Device-node labeling is
separate: use the device-policy diagnostics in Devices and host permissions.

### Slow first launch or blank terminal

Cold creation performs package and plugin downloads. Timed stage output and
the temporary TUI loading line indicate progress. Warm starts retain compatible
dependency state. Host SDK manifests older than runtime are normalized inside
the copied config, not by modifying host configuration. Preserve the environment
when diagnosing a wait; reset discards the OS cache and can repeat cold setup.

### Active workspace lock or failed import

Exit the active launcher before sync/reset/repair/maintenance/purge. Ordinary V2
TUI, session shell terminals and web clients can join the live owner. Status and
stop remain available in another terminal. Do not delete the lock file to bypass a live
process. For a failed import, retry the same flag with an updated launcher;
recovery uses the saved export without overwriting existing container work.
For a failed archive sync, use `sync`; the original live DB remains in its
volume. A deleted private volume cannot be recovered from session metadata alone.

### Maintenance shell environment

Use `oc-box terminal --maintenance` to debug an existing container without launching OpenCode
or supplying host tokens. A missing command may need installing in that
container; terminal does not install baseline tools automatically. Its shell
deliberately skips user profiles, shell history, and normal credential forwarding.
A missing project marker or changed identity still requires explicit `repair`;
terminal cannot bypass those checks. If it reports an active launcher, exit
OpenCode and wait for its history sync/stop before retrying. Do not remove the
workspace lock file to bypass the running process.

## Installation and manuals

Requires Linux, Python 3.10 or later, rootless Podman with pasta networking,
delegated cgroup CPU/memory/PIDs controllers, and host OpenCode 1.x or 2.x. Session
import/export compatibility was checked with OpenCode 1.19.5 and 2.2.6. Other major
OpenCode versions are rejected rather than assuming database/CLI compatibility.
Podman must work from the regular host user, not through sudo. Network access
is needed for initial distribution, runtime, and dependency downloads.

### Bash completion

Enable command/option completion in the current Bash session, for any
installation channel (including uv and pipx):

```bash
source <(oc-box completion bash)
```

Add that line to `~/.bashrc` after the PATH setup for oc-box to enable it in new
interactive Bash shells. The installer never edits your shell startup files.
Alternatively, with the optional bash-completion framework enabled, save the
script in its per-user autoload directory:

```sh
mkdir -p "${XDG_DATA_HOME:-$HOME/.local/share}/bash-completion/completions"
oc-box completion bash > "${XDG_DATA_HOME:-$HOME/.local/share}/bash-completion/completions/oc-box"
```

The framework's OS package is normally `bash-completion` on all seven supported
distros. It must itself be enabled by your distro's Bash startup setup for
autoloading; directly sourcing the script works without that framework. Reload
with the `source` command after an upgrade in an already-open shell. If you saved
a copy manually from a uv/pipx install, regenerate that copy after upgrading.

GitHub/source installs place it in
`~/.local/share/bash-completion/completions/oc-box` and refresh it on upgrades.
They also retain a release-local copy at `completions/oc-box`. Wheels contain the
same package resource and install another copy under their Python environment's
`share/bash-completion/completions/oc-box`; uv/pipx private environments may not
be on Bash's autoload search path, so use either activation method above.
From a source checkout, `source completions/oc-box` works directly in Bash.

Completion suggests commands, context-appropriate options, vault actions,
distros/aliases, doctor features, auto-update and credential-mode choices.
Credential modes include `env`, `selected`, and `vault`; `--no-vault` is offered
for start/reset/web only (including the default start context). Completion does
not inspect the OpenCode major version or whether a service is already active;
the command itself rejects V1 or attached-client use of `--no-vault`.
`--host-drop` and `--no-host-drop` startup flags are offered only for
default/start/reset TUI clients, not web, terminal or the separate dashboard.
The clipboard enable/disable and symlink startup flags use the same command
scope. The separate `preferences --host-drop`, `--host-clipboard` and
`--host-clipboard-symlinks` settings complete `on`/`off`.
Direct-TTY, native-version and
host-input requirements are checked by the command, not during completion.
`--project` and `--log-dir` complete directories; backup archive inputs,
`--output`, and `--identity` complete paths, including spaces. Both
`--option VALUE` and `--option=VALUE` are supported. Press Tab after `oc-box `
or `oc-box backup --` to see candidates. Completion uses only Bash builtins and
local pathname expansion: it does not run oc-box on each Tab, query Podman,
contact providers, read vault contents, or guess passwords, credential names,
session/workspace IDs, or model identifiers.

### Python package installations

Selected releases are available at https://pypi.org/project/oc-box/. Choose one
package-managed CLI installation, not system pip:

```sh
pipx install oc-box
pipx ensurepath
# Or:
uv tool install oc-box
uv tool update-shell
```

Open a new terminal if those commands add the tool-manager bin directory to
PATH. For standard pip, create and activate a dedicated virtual environment:

```sh
python3 -m venv ~/.venvs/oc-box
. ~/.venvs/oc-box/bin/activate
python -m pip install oc-box
oc-box --version
```

On later visits, activate it again or call `~/.venvs/oc-box/bin/oc-box` directly.

Podman, OpenCode, Linux/cgroup configuration, and device drivers are external
host requirements, not Python dependencies. Package installation never creates
the curl installer's `~/.local/share/oc-box` release tree or a competing global
wrapper. pipx/uv expose the package's console entrypoint in their normal bin
directory. `python -m oc_box` is also available inside the package environment.

For a release containing the experimental Textual manager, install the optional
extra instead of the bare package:

```sh
uv tool install 'oc-box[tui]'
# Or:
pipx install 'oc-box[tui]'
```

Use `'oc-box[tui,vault]'` to request both extras. The initial dashboard ranges
are `textual>=8.2.8,<9` and `pyte>=0.8.2,<0.9`; normal non-TUI commands do not
require them. Existing published releases need not include this development
feature. Keep optional dependencies in the tool's isolated environment, not
system/user-site Python.

Upgrade through the owner of the installation:

```sh
pipx upgrade oc-box
uv tool upgrade oc-box
# Inside the installed virtual environment:
python -m pip install --upgrade oc-box
```

The regular GitHub/curl installer and updater remain supported. Both channels
read the same workspace registry, projects, and private containers. If several
launchers exist, use `command -v oc-box` to check which PATH entry is selected;
avoid keeping conflicting launchers in the same bin location. Source commits
continue to publish on GitHub independently of selected PyPI releases.

Wheels include runtime scripts, prebuilt manuals, license/notice, and source
metadata in the isolated `oc_box` namespace. Manuals also install under the
Python environment's `share/man/man1` and `share/info` directories; tool-manager
environments are not necessarily in the host readers' default search path.
For example, after activating the relevant environment:

```sh
man "$VIRTUAL_ENV/share/man/man1/oc-box.1"
info --file "$VIRTUAL_ENV/share/info/oc-box.info"
```

No source checkout, setuptools, documentation build tools, or third-party Python
runtime dependency is required for the default CLI in an installed wheel.
Vault/TUI features have explicit optional dependencies. For building or
publishing, see CONTRIBUTING.md: GitHub's `publish.yml` workflow uses the `pypi`
environment and PyPI Trusted Publishing without long-lived upload tokens.

### GitHub/curl installations

One-line installation from the public GitHub branch requires curl, Git, and
Python 3.10+ already available on the host. It installs oc-box, not Podman or
host OpenCode, and executes the downloaded installer as your user:

```sh
curl -fsSL --proto '=https' --tlsv1.2 https://raw.githubusercontent.com/groopcd/oc-box/master/install.sh | sh
```

To inspect before execution instead of piping to a shell:

```sh
curl -fsSL --proto '=https' --tlsv1.2 https://raw.githubusercontent.com/groopcd/oc-box/master/install.sh -o install-oc-box.sh
# Review install-oc-box.sh, then:
sh install-oc-box.sh
```

For a reviewed installer containing the experimental dashboard, enable its
optional dependencies with `sh install.sh --devel --with-tui`, or
`sh install.sh --devel --with-vault --with-tui` to include vault support. Use
the actual downloaded filename (`install-oc-box.sh` above). A piped bootstrap
accepts `sh -s -- --devel --with-tui` or
`sh -s -- --devel --with-vault --with-tui`. Dependencies are installed privately
in the immutable release; no system/user-site pip is used.
GitHub updates retain these opt-ins while existing launchers remain pinned to
their earlier release. Python-package installations instead use their extras
and owning package manager.

These are first-install/bootstrap options, not a requirement to rerun curl when
enabling extras later. On a launcher release supporting upgrade extras, use
`oc-box upgrade --devel --tui`, `oc-box upgrade --devel --vault`, or both flags
through the existing owner. Older channel-aware launchers should upgrade normally
to a supporting published release first. The update-channel guide documents
pre-channel exceptions.

The bootstrap clones full history anonymously into a private temporary directory
and removes that temporary checkout when finished. It does not configure Git
authentication, download credentials, or keep a source checkout in your project.
When moving from an older flat installation, exit its active oc-box launchers
first; future versioned installations can coexist with already running versions.

Alternatively install from the source checkout as your host user:

```sh
python3 install.py
oc-box --version
man oc-box
info oc-box
```

The installer copies prebuilt manual files and does not require makeinfo,
groff, man, or info. Reading them needs the host reader; on openSUSE those are
the `man` and `info` packages. It updates only the user's Info index when an
`install-info` utility is available, not any system index. Ensure
`~/.local/bin` is on PATH. If local documentation directories are not searched:

```sh
MANPATH="$HOME/.local/share/man:" man oc-box
INFOPATH="$HOME/.local/share/info:" info oc-box
man "$HOME/.local/share/man/man1/oc-box.1"
info --file "$HOME/.local/share/info/oc-box.info"
```

The full CLI reference is `docs/cli.md`; the source tree includes generated
`docs/oc-box.1`, `docs/oc-box.texi`, and `docs/oc-box.info`. Maintainers regenerate
them with `python3 tools/build_docs.py`; that build requires GNU makeinfo.
`python3 tools/build_docs.py --check` checks the generated man/Texinfo source.
End users do not need to rebuild documentation to install it.

## Authors and license

Author: Paulo Dias <paulo.miguel.dias@gmail.com>. AI-assisted implementation,
debugging, tests, and documentation: OpenAI coding assistant via OpenCode.
Copyright (C) 2026 Paulo Dias. Original oc-box source and documentation are
licensed GPL-3.0-or-later, without warranty; retain LICENSE and NOTICE.
OpenCode, Podman, distribution images, MCPs, and other installed programs
retain their own licenses. Documentation does not alter their behavior.

## See also

`podman(1)`, `podman-create(1)`, `podman-stats(1)`, `podman-update(1)`,
`containers.conf(5)`, `containers-storage.conf(5)`, `info(1)`, `man(1)`.
The project README provides setup context and development verification commands.
Use `oc-box --help` to discover the installed launcher's commands and
`oc-box COMMAND --help` for a command's options.
