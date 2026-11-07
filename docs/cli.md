# oc-box command reference

## Name

oc-box - persistent rootless Podman environments for mainline OpenCode.

## Synopsis

```text
oc-box [start] [--project PATH | --workspace ID] [START-OPTIONS]
oc-box stop [--project PATH | --workspace ID]
oc-box sync [--project PATH | --workspace ID]
oc-box reset [--project PATH | --workspace ID] [RESET-OPTIONS]
oc-box repair [--project PATH | --workspace ID]
oc-box terminal [--project PATH | --workspace ID] [--maintenance]
oc-box purge [--project PATH | --workspace ID] [--delete-sessions | --all] [-y]
oc-box list
oc-box distros
oc-box doctor [--json] [--feature FEATURE ...]
oc-box completion bash
oc-box web [--project PATH | --workspace ID] [--port N] [--no-open]
oc-box --status [--project PATH | --workspace ID] [--json]
oc-box status [--project PATH | --workspace ID] [--json]
oc-box upgrade [--check | --auto-update on|off]
oc-box secrets init|list|passwd
oc-box secrets set|remove NAME
oc-box credentials [--project PATH | --workspace ID] [CREDENTIAL-OPTIONS]
oc-box backup [--project PATH | --workspace ID] --output FILE [--portable] [--recipient AGE_KEY ...]
oc-box backup-info FILE [--identity FILE ...]
oc-box restore FILE --project NEW_PATH [--identity FILE ...]
oc-box --about
oc-box --version
oc-box -h | --help
```

The command defaults to `start`. Place options before or after the command.
Run the launcher on the host as your regular user, without sudo. It manages
rootless Podman; container root is mapped to the unprivileged host user.

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
Launching oc-box again resumes the original container session. Running
processes do not survive a stop. Host OpenCode and its configuration are
unchanged. Build mode permits all OpenCode tool permissions inside oc-box;
Plan mode and subagents retain their own rules.

The default resource limits are 4 CPU cores, 8 GiB RAM, and 2048 processes.
These are ceilings, not reservations: resources are consumed dynamically up to
those limits, and OpenCode itself has no fixed 4/8 GiB minimum requirement.
There is no disk quota. Containers share the host kernel and can reach network
services, including configured host services at `host.containers.internal`.
The project is intentionally writable and can be deleted by container commands.
Additional Podman options and devices explicitly extend these defaults.

### OpenCode versions

This compatibility branch supports OpenCode 1.x and 2.x, tested with 1.19.5 and
2.2.6. The host release selects the exact container package: `opencode-ai` for
1.x, `@opencode/cli` for 2.x. V2 uses a workspace-owned private stdio server with
native `--server` clients and current env-file/vault selections at server startup;
isolated setup/history probes use `--standalone`. The host shared service is
never restarted or exposed inside the container. Its service settings are not
copied. V2 uses a
separate container database/resume state seeded from a consistent V1 snapshot
and migrated by upstream; V1 history is retained for downgrade.

Native V2 plugins use the new API; copied V1 local plugins get bounded adapters
for shell environment, tool execution/output/definition, text custom tools and
provider discovery. Unsupported hooks still require native ports. Supported
legacy config and V2 fields are retained; the Build plugin is version-aware.
MCP Code Mode and native formatting are preserved. Upstream 2.2.6 does not run language servers
or expose LSP diagnostics; native LSP remains on 1.x. See `docs/opencode-2.md`
for environment strategy, migration limits, and pending host validation.

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
oc-box doctor --feature backup --feature vault
oc-box doctor --feature backup-info --json
```

Feature selectors: `launch`, `maintenance`, `history`, `backup`, `backup-info`, `restore`,
`vault`, `github-upgrade`, `bootstrap`, `storage`, `overlay`, `manuals`, and
`docs-build`. Without selectors all are reported, but exit status checks only
core `launch` prerequisites. With selectors, exit 0 means every selected
feature's prerequisites are present; exit 1 means at least one is missing or
incompatible. JSON schema version 1 includes scope, detected distro, feature
availability, dependency details, missing providers, package names, installation
commands, and notes. Missing optional features never disable unrelated commands.
Feature commands check their prerequisites before secret prompts or destructive
workspace changes and include OS-specific fixes.

This checks installed prerequisites, not full runtime certification. Podman
still checks rootless mode, subordinate IDs, controller delegation, and mounts;
interactive commands check terminal requirements. Command presence does not
establish a compatible version or validate every backend/driver configuration.
Normal OpenCode launch verifies its host/container versions separately.

### start

Create or resume an environment. This is the default when no command is given.
The project directory must already exist and be dedicated to the project.
Without a selector, use the current directory. With `--workspace`, use the
registered directory; a workspace ID takes precedence if both selectors are
provided. Do not combine selectors when a single selector will do.

On first launch, detect the host distro, create a labeled container and private
home volume, install baseline tools, install the exact host OpenCode release,
copy configuration, resolve plugin dependencies, and start a conversation.
Afterward, start the existing environment and resume its latest recorded root
conversation. `--new` starts another conversation in the same environment.

```sh
mkdir -p ~/devel/demo
oc-box --project ~/devel/demo
oc-box start --project ~/devel/demo --new
```

Cold startup includes image/package/plugin downloads; timed stage messages
appear every five seconds. A loading line covers blank TUI startup frames and
disappears when OpenCode draws text. Warm launches retain compatible caches.
The container OpenCode version is verified against the host on every launch;
exact upgrades and downgrades are supported, with no fallback to another release.

### stop

Stop a registered container without deleting the project, container OS, private
home, session IDs, or metadata. Already stopped or missing containers need no
start. This command can run from a second host terminal while OpenCode is active.
Its processes stop, and the active client may disconnect or report tool errors.
Exit that launcher before starting another instance or resetting the workspace.
Use `sync` afterward if a final history export was missed.

```sh
oc-box stop --project ~/devel/demo
```

### sync

Refresh host conversation archives without opening the TUI. Requires an
existing workspace and an intact project directory. It starts or recreates the
container if needed, prepares its runtime/configuration, discovers saved root
sessions, exports them, and imports separate namespaced archive copies into the
host database. It stops the container afterward, even if it had been running.

An active oc-box launcher holds a workspace lock, so exit it before syncing.
Use this after an OpenCode crash, missed exit-time sync, or host restart.
Sync failure preserves container data; retry after resolving the error.

```sh
oc-box sync --project ~/devel/demo
oc-box sync --workspace WORKSPACE_ID
```

### web — browser interface

Open the native OpenCode 2.x browser UI for a workspace. If its TUI or web owner
is running, attach to that same private service/session without waiting for it
to exit or unlocking/refreshing credentials. Otherwise perform a normal V2
startup (including selected credential unlock) and own a foreground web lease.
New projects can be created using the host's default distro. Requires host
OpenCode 2.x; 1.x has no web command through this launcher.

```sh
oc-box --project ~/devel/demo
# While it runs, use another host terminal:
oc-box web --project ~/devel/demo
oc-box terminal --project ~/devel/demo
```

The browser uses upstream's short-lived one-use pairing link and opens the same
session through the native web app. A host relay binds only `127.0.0.1`, forwards
HTTP/streaming/WebSocket bytes through Podman exec to container loopback, and
does not publish a container port, allow LAN access, or join the host OpenCode
service. Links grant access to the workspace service; do not share or log them.
`--no-open` prints the link without launching a browser. `--port` defaults to 0
(choose a free loopback port). Keep the web command open; Ctrl-C closes that
relay. Browser commands use a sanitized host environment without provider/vault
values; the server retains the owning launch's credential snapshot.

The owner keeps the lifecycle lock and waits for attached terminal/web/TUI
clients before final history sync and container stop. Closing one attached
client does not stop the others. `oc-box stop` from another host terminal stops
the whole workspace and disconnects all clients. Backup/reset/purge continue to
require the lifecycle lock. Run normal `oc-box start --project PATH` to join a
running web-owned session with the full TUI. Additional clients accept selectors
(and start's `--resume`) only; change model/session/configuration when no owner
is active. The owning foreground command is needed for web access; closing the
browser tab alone does not close its relay or terminate the owner.

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
`--workspace ID`. Only these selectors, `--maintenance` and help are accepted. Run from a real
host terminal with TTY input/output; piped or redirected input/output is refused.

```sh
oc-box terminal
oc-box terminal --project ~/devel/demo
oc-box terminal --workspace WORKSPACE_ID
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
machine-readable output. See Resource reports for metric interpretation.

```sh
oc-box status
oc-box --status --project ~/devel/demo
oc-box --status --json
```

### upgrade

Check and update the installed oc-box launcher/modules and prebuilt manuals
from the fixed public GitHub repository, branch `master`. Run on the host as
your regular user; no sudo or GitHub token is needed. This command works without
Podman or a registered workspace. It does not update host OpenCode, modify a
source checkout, reset a container, or touch project/session data.

This command's GitHub update path applies only to curl/source-installed copies.
Python-package installations detect their packaged distribution marker and
print `pipx upgrade oc-box`, `uv tool upgrade oc-box`, or the current interpreter's
pip upgrade command instead, without downloading or installing anything. They
reject `--auto-update on` because a package manager owns those files; normal
launches skip GitHub automatic updates even if a shared host config enables them.
`--auto-update off` on a package-managed copy prints that package guidance and
does not modify the GitHub installation's shared setting. Both channels use the
same runtime/workspace format and remain supported in parallel.

```sh
oc-box upgrade --check
oc-box upgrade
oc-box upgrade --auto-update on
oc-box upgrade --auto-update off
```

`--check` compares the installed commit with GitHub and reports whether it
differs, without installing. A normal upgrade clones the full public branch
history into a temporary directory, pins the checked commit, verifies source
identity, and runs its staged user-local installer. An identical clean build
does not download the full history again. A locally installed build whose commit
count is ahead of the published branch is retained rather than downgraded.
Source-checkout updates remain a separate `git pull`/review task: upgrade always
targets the installed user-local copy. A changed published commit at the same
count can replace a different installed build.

The installer stages a complete release under `~/.local/share/oc-box/releases/`,
including license and manuals, before switching the launcher. New processes use
the new version; active launchers keep their original release path and lazy-load
its original modules. Old release directories are retained and have no automatic
garbage collection. `current` and compatible top-level installed paths point to
the activated release. An older flat installation is migrated; its old manuals
are preserved as `legacy-docs`. No workspaces or credentials are migrated/deleted.

Automatic updates are off by default. `--auto-update on` saves `auto_update: true`
in the existing host oc-box configuration, retaining settings such as `pass_env`.
Normal installed `start` launches check before Podman/workspace setup and, when
updated, continue with the same arguments. No background daemon, active-session
update, or automatic check for list/status/terminal/reset/purge is introduced.
Direct source-checkout launches skip automatic updates. A concurrent check does
not block another normal launcher. Network/download failures are reported and
the normal launch continues with the installed version; explicit upgrade failures
return nonzero. After enabling automatic updates you are trusting future code
published to this development branch, not signed release artifacts.

Fetch/checkout/install subprocesses have a clean, positive-allowlisted
environment. Global/system Git config, helpers, hooks, external protocols,
provider tokens, SSH agents, and user proxy variables are not used by the public
download. HTTPS is required; shell commands or a custom upstream URL are not
accepted. Use manual source installation for a reviewed alternate deployment.
Updates inside a container are refused because its private HOME is not the host
installation. Git is required for public updates; build tools are not.

### secrets

Manage an optional encrypted credential vault on the host. This command never
starts Podman or copies secrets into the development environment. The existing
env-file workflow is unchanged unless you select vault credentials explicitly.
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
pipx install 'oc-box[vault]'
# Add support to an existing pipx installation:
pipx inject oc-box 'cryptography>=44'
# Or install/reinstall the uv tool with its extra:
uv tool install --force 'oc-box[vault]'
# In a package installation's activated virtual environment:
python -m pip install 'oc-box[vault]'
```

For GitHub/source installations, `python3 install.py --with-vault` stages the
optional crypto wheels privately in that release (using a temporary venv), not
in system/user-site Python. The curl bootstrap accepts `sh -s -- --with-vault`
when piped to a shell. Future GitHub upgrades preserve that opt-in. The source
can also use a host Python with a distro-provided cryptography package. A
missing dependency yields guidance, not a fallback to weak/custom encryption.
No desktop keyring, external password manager, cloud service, or daemon is used.

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
During vault-enabled launches, host captured subprocess output uses anonymous
Linux memfd storage rather than disk-backed temporary files. Launcher-generated
failure messages redact exact active vault values. This is not a general
redaction filter for OpenCode, provider logs, tool output, or session archives:
an agent can still print, save, or transmit a credential it has received.

### credentials

View or change **names-only** forwarding policy for an existing workspace. The
default is `env`: auto-forward sourced token/API-key variables and configured
`pass_env` exactly as before. No policy change occurs merely by upgrading.
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
it, exit, then configure selections. Use only selectors and credential options;
session, device, resource, deletion, or update options are refused.

In `selected` mode, only the explicitly selected env/vault names are delivered,
plus the launcher's fixed runtime settings and noncredential language/color/
model-routing preferences. This replaces automatic suffix forwarding and global
`pass_env`, not just supplements them. Vault and env names cannot overlap.
Uppercase credential names cannot override loader/shell/host runtime settings
such as PATH, BASH_ENV, LD_PRELOAD, OPENCODE_AUTH_CONTENT, or OC_BOX variables.
Names append/deduplicate; `--clear-credentials` clears both lists before adding.

```sh
source /path/to/private/file.env
oc-box credentials --project ~/devel/demo --credential-mode selected \
  --clear-credentials --env-secret GITHUB_TOKEN --vault-secret BRAVE_API_KEY
oc-box --project ~/devel/demo
oc-box credentials --project ~/devel/demo --credential-mode env --clear-credentials
```

A normal start/reset/import launch unlocks once when selected vault names exist,
and the decrypted selection is kept only in the launcher's memory/context. It
is not written to the workspace metadata, host os.environ, command arguments,
config files, or a cache. Values reach the container through Podman's inherited
env names. Wrong passphrase/missing selection aborts before container setup or
reset deletion. Env mode may also supplement inherited variables with selected
vault names; a vault value then wins over the inherited same-named env value.

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
the host-wide vault, host global DB/config, or arbitrary paths outside those
components. Env files inside the selected project are project data and are
included; host env files outside it are not read.

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
registry, and private home. New output is published atomically without replacing
an existing file and has 0600 permissions. Expect enough free space for staged
ciphertext, final output, and a temporary committed image. Failure leaves no
final partial backup; the source remains stopped, not reset/deleted. The
temporary capture image is removed by its unique tag; shared base images remain.

### backup-info

Decrypt/verify a backup's manifest and ciphertext checksums without starting
Podman or extracting workspace contents. Requires age and a controlling terminal
for passphrase backups, or a native private --identity file for recipient backups.
Output is JSON metadata including kind, source path/ID, architecture, component
sizes/checksums, saved session IDs, settings, and documented limitations. Raw
additional Podman option values are omitted to avoid printing saved secrets.
It never prints archived file contents or the host vault and does not execute
container image code. Metadata itself may contain private paths/session names.

```sh
oc-box backup-info /backup/demo.ocbox
oc-box backup-info /backup/demo.ocbox --identity ~/.config/backup-age-key.txt
```

Inspection stages only encrypted envelope components while checking them.
It authenticates the manifest and binds component ciphertext to it; restore
also verifies every age data stream through its final authentication chunk.
Wrong keys, corruption, missing/extra components, unsupported versions, or unsafe
outer envelope paths are rejected before restore activation.

### restore

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

The host vault is not included or unlocked. Credential selections retain names
only, so provide matching host env/vault credentials before normal selected-mode
startup. Copied auth/config and secrets stored in project/home/OS are part of
the encrypted payload and remain present after restore. A failed operation
cleans only its newly created project/volume/container/registry and preserves
original environments; interruption may retain staging marked incomplete, which
normal launch refuses until the operator inspects/retries. Loaded OCI images
may remain in shared Podman image cache after failure; no global prune is used.
The host registry journals fresh resource IDs and `restore_staging_project`
before payload extraction. An interrupted or failed-cleanup entry has
`restore_incomplete: true`; list/status can expose its ID without launching it.
Inspect the staging location, use plain `purge --workspace ID` for its managed
resources (which skips history sync for incomplete restores), and retry to a new
destination. Orphan staging directories may need manual host removal after
inspection; the launcher never deletes a caller-supplied tree on marker alone.

## Options

### -h, --help

Print command choices, options, and short descriptions; exit without Podman.
For example: `oc-box --help`.

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

### --port N

Web-only. Select the host-loopback relay port: 0 (default) chooses a free port;
explicit ports are 1..65535. Binding failure is reported without replacing an
existing listener. The container service's own random loopback port stays private.

### --no-open

Web-only. Print the native one-use browser pairing link instead of invoking the
host browser. The foreground relay still runs until Ctrl-C or workspace stop.

### --project PATH

Select an existing project directory; relative paths resolve from the host
working directory. Quote paths containing spaces. For lifecycle commands the
default is the current directory. For status, omission means all workspaces.
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

### --status

Report CPU, RAM, storage, state, limits, and sessions without opening OpenCode.
Equivalent to `status`. Only selectors and `--json` apply. It cannot be combined
with another command, session mode, resource change, or deletion action.

### --json

Doctor also accepts this flag to emit its versioned feature/dependency report.

Emit status as one JSON object with `sampled_at` and `workspaces`. Sizes are
numeric bytes and unavailable values are `null`; notes explain partial results.
Progress goes to stderr, not the JSON stream. Status uses this schema; doctor
uses the feature/dependency schema described under that command.

```sh
oc-box --status --json > status.json
```

### --about

Print oc-box version, author Paulo Dias and email, OpenAI coding-assistant
contribution, GPL-3.0-or-later licensing, and source commit. No Podman or
workspace is required. Use alone, without lifecycle selectors/options.

### --version

Print oc-box's commit-derived version and exit without Podman. The format is
`1.0.N+gHASH`, with N the full-history commit count minus 29 (the 1.0.0
baseline). Each later commit advances N; shallow history cannot supply a release
identity. Tracked uncommitted changes add `.dirty`. Untracked files do not. Installed
copies retain their installation-time identity in `build-info.json`; reinstall
to update it. This is separate from the host OpenCode version matched inside
containers. Use alone, for example: `oc-box --version`.

PyPI distribution metadata uses only `1.0.N`, because public PyPI versions
cannot contain the local `+gHASH` suffix. Its packaged build-info still includes
the full source identity displayed by this command, so the exact Git commit is
retained even in a wheel/source distribution installed without Git.
Existing 0.1 installations retain their original embedded identity until
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

With `upgrade`, report whether the public GitHub commit differs from the
installed version without installing it. Returns success when a version is
available or the installation is current; it is not an update-available exit
code. Network errors return nonzero. Cannot be combined with `--auto-update`,
workspace selectors, or other commands. Example: `oc-box upgrade --check`.

### --auto-update on|off

With `upgrade`, persistently enable or disable automatic updates before normal
installed launches. The default is off. This setting itself performs no download
or installation and preserves other keys in `~/.config/oc-box/config.json` (or
its XDG equivalent). Cannot be combined with `--check` or workspace actions.
Example: `oc-box upgrade --auto-update on`.

### --new

Start a new root conversation while retaining the same container and files.
Use with start (the default) or reset. Mutually exclusive with `--resume` and
`--import-session`. Example: `oc-box --project ~/devel/demo --new`.

### --resume

Explicitly request the normal resume behavior. Requires a registered workspace;
if one exists but no root session has been recorded, OpenCode can start a new
conversation. Use with start/reset. Mutually exclusive with `--new` and
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

Pass the provider/model choice to the OpenCode TUI on this launch. It is not a
persistent oc-box workspace setting. Use with start/reset; otherwise normal
OpenCode provider/model preferences apply.

```sh
oc-box --project ~/devel/demo --model github-copilot/gpt-6.1-sol
```

### --credential-mode env|selected

With `credentials`, choose forwarding mode for that workspace. `env` is the
unchanged default: suffix-based and custom pass_env forwarding, optionally
supplemented with selected vault names. `selected` forwards only configured
credential names, plus fixed runtime/nonsecret routing settings. The mode is
saved in host metadata, not applied as a container reset or to an active client.
Example: `oc-box credentials --project ~/devel/demo --credential-mode selected`.

### --env-secret NAME

With `credentials`, append an uppercase environment credential name to the
workspace's explicit env selection, repeatable and deduplicated. In selected
mode it must be present/nonempty in the sourced host environment before launch;
a missing value aborts before unlock/setup. Env mode already forwards legacy
variables, so this list becomes restrictive only in selected mode. No value is
accepted here. Example: `oc-box credentials --env-secret PERSONAL_GITHUB_TOKEN`.

### --vault-secret NAME

With `credentials`, select an existing encrypted vault entry name to unlock and
forward on normal start/reset/import launches. Repeat for multiple names. The
name is saved in workspace metadata, never its secret value. Vault presence is
checked when unlocking, not by this names-only configuration command. A name
cannot also be selected with --env-secret. Example:
`oc-box credentials --vault-secret BRAVE_API_KEY`.

### --clear-credentials

With `credentials`, clear both saved env-name and vault-name lists before adding
new selections on this invocation. It does not erase the vault, remove provider
auth/config, revoke credentials, or change mode unless --credential-mode is also
specified. Example: `oc-box credentials --credential-mode env --clear-credentials`
returns to the original env workflow without deleting encrypted vault entries.

### --output FILE

Backup-only. Required new encrypted backup output pathname. Its parent must
already exist outside the project/registry/private volume. Existing files or
symlinks are never overwritten, including a destination created during capture.
Example: `oc-box backup --project ~/devel/demo --output /backup/demo.ocbox`.
The archive suffix is conventional; the versioned format is checked on restore.

### --portable

Backup-only. Omit the container OS image while retaining project, complete
private home, sessions, registry evidence, and limits/credential-name settings.
Restore rebuilds the selected distro OS/baseline tools; custom system packages
and native-cache compatibility are the user's responsibility. Example:
`oc-box backup --project ~/devel/demo --portable --output /backup/demo.ocbox`.

### --recipient AGE_KEY

Backup-only, repeatable (up to 16). Encrypt the backup key envelope to a native
age X25519 public key, allowing backup without a password prompt. Any matching
private identity can decrypt it. Without this option age requests a backup
passphrase interactively. Public recipients are not secret; private identities
must be stored independently of the archive and project. Example:
`oc-box backup --recipient age1PUBLIC_KEY --output /backup/demo.ocbox`.

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

```sh
oc-box reset --project ~/devel/demo --cpus 8
```

### --memory SIZE

Set the memory limit using Podman size syntax, such as `512m`, `8g`, or `16g`.
Default: `8g`. Set at creation or reset; not a live update. The value is saved
for future launches. Limits require delegated cgroup controllers. Podman
validates the size and kernel support. `0` requests no memory limit where
supported. There is no automatic project/volume disk quota.

```sh
oc-box reset --project ~/devel/demo --memory 16g
```

### --pids-limit NUMBER

Set the container's process limit; default `2048`. oc-box requires at least
`32`. Set at creation or reset; changing an existing container requires reset.
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
| Restore | Backup prerequisites plus pasta, init/helper/runtime providers for container creation |
| Optional vault | `cryptography` in the same Python environment as oc-box; no age requirement |
| GitHub installation/upgrade | Git and Python; one-line download also needs curl, mktemp and trusted CA certificates |
| Storage reports | GNU `du` from `coreutils`; BusyBox du is insufficient; other metrics still work if absent |
| FUSE overlay driver | `fuse-overlayfs` only if selected/needed; kernel overlay does not require it |
| Read manuals | man (`man`, `man-db`, or Alpine `mandoc`) and info (`info` or `texinfo`) |
| Regenerate manuals | GNU makeinfo (`texinfo`); contributor-only, never required to install/use oc-box |

Host OpenCode is not supplied by these mappings; see
https://opencode.ai/docs/#install. Use the existing Python installation channel
for vault support: `uv tool install --force 'oc-box[vault]'`,
`pipx inject oc-box 'cryptography>=44'`, or GitHub/source `--with-vault`.
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
configured, plus native LSP on 1.x only. A container-only plugin sets Build permissions to allow
all. Plan/subagent and plain host permissions are unchanged. Other copied
plugins may have independent checks. Changes require exiting and restarting
OpenCode; config is not hot-reloaded. Host-only executables must be installed
inside the container. Host provider/MCP auth is copied when available; refreshed
container auth is not written back to host auth.

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

Variables ending `_API_KEY` or `_TOKEN`, plus `GH_TOKEN`, `GITHUB_TOKEN`,
terminal color/language settings, and `LEMONADE_BASE_URL` are passed when set.
These forwarding rules apply to normal OpenCode launches, not `terminal`.
Add other uppercase environment names in host configuration:

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

`OPENCODE_AUTH_CONTENT` can supply provider auth to copy into the container.
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

### Host registry and archives

`~/.local/state/oc-box/workspaces/ID/workspace.json` holds directory identity,
container/volume names, distro, limits, and saved Podman/device settings.
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

### Container data

The home volume is `oc-box-home-ID`, mounted at the host user's home pathname.
OpenCode data lives under its `.local/share/opencode/`. Copied configuration and
generated `oc-box-profile.json` live under `.config/opencode/`. The matching
OpenCode executable is `/opt/oc-box/runtime/bin/opencode`; Node/npm, Python/uv,
Bash, Git, ripgrep, curl, archive tools, and PDF tools are provided by baseline
setup. Additional system tools belong to the container OS layer.

## Exit status

`0` means successful informational/lifecycle work or normal TUI exit. A TUI
launch normally returns OpenCode/Podman's exit code. oc-box validation, backend,
setup, and sync failures return `1`; a successful TUI followed by failed history
sync also returns `1` and retains container data. Argument syntax errors return
`2`. A Python KeyboardInterrupt handled by the launcher returns `130`.
An exception during final cleanup can also cause a nonzero result.

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
`--project` completes directories; backup archive inputs, `--output`, and
`--identity` complete paths, including spaces. Both `--option VALUE` and
`--option=VALUE` are supported. Press Tab after `oc-box ` or `oc-box backup --`
to see candidates. Completion uses only Bash builtins and local pathname
expansion: it does not run oc-box on each Tab, query Podman, contact providers,
read vault contents, or guess passwords, credential names, session/workspace
IDs, or model identifiers.

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
runtime dependency is required to use an installed wheel. For building or
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
Use `oc-box --help` for the installed launcher's actual command-line surface.
