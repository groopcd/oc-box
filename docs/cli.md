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
oc-box terminal [--project PATH | --workspace ID]
oc-box purge [--project PATH | --workspace ID] [--delete-sessions | --all] [-y]
oc-box list
oc-box distros
oc-box --status [--project PATH | --workspace ID] [--json]
oc-box status [--project PATH | --workspace ID] [--json]
oc-box upgrade [--check | --auto-update on|off]
oc-box --about
oc-box --version
oc-box -h | --help
```

The command defaults to `start`. Place options before or after the command.
Run the launcher on the host as your regular user, without sudo. It manages
rootless Podman; container root is mapped to the unprivileged host user.

## Description

oc-box starts unmodified OpenCode and its local MCP servers, plugins,
formatters, language servers, shell, and file tools together inside a persistent
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

## Commands

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

### terminal

Open an interactive maintenance shell in an existing workspace container
instead of starting OpenCode. Select the current project, `--project PATH`, or
`--workspace ID`. Only these selectors and help are accepted. Run from a real
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
oc-box terminal --project ~/devel/npu
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

## Options

### -h, --help

Print command choices, options, and short descriptions; exit without Podman.
For example: `oc-box --help`.

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

Emit status as one JSON object with `sampled_at` and `workspaces`. Sizes are
numeric bytes and unavailable values are `null`; notes explain partial results.
Progress goes to stderr, not the JSON stream. Applies only to status.

```sh
oc-box --status --json > status.json
```

### --about

Print oc-box version, author Paulo Dias and email, OpenAI coding-assistant
contribution, GPL-3.0-or-later licensing, and source commit. No Podman or
workspace is required. Use alone, without lifecycle selectors/options.

### --version

Print oc-box's commit-derived version and exit without Podman. The format is
`0.1.N+gHASH`, with N the full-history commit count. Each commit advances N;
tracked uncommitted changes add `.dirty`. Untracked files do not. Installed
copies retain their installation-time identity in `build-info.json`; reinstall
to update it. This is separate from the host OpenCode version matched inside
containers. Use alone, for example: `oc-box --version`.

PyPI distribution metadata uses only `0.1.N`, because public PyPI versions
cannot contain the local `+gHASH` suffix. Its packaged build-info still includes
the full source identity displayed by this command, so the exact Git commit is
retained even in a wheel/source distribution installed without Git.

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
terminal               kept      kept; no archive sync  kept; prior run state
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
An active launcher holds a nonblocking workspace lock; start, sync, reset,
repair, terminal, and purge require that launcher to exit. Read-only list/status and
explicit stop can run from a second terminal. A crash releases the lock when
its process exits; an orphan still running can retain it.

## Distribution images

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

The generated profile selects Bash and enables native formatters/LSP when not
explicitly configured. A container-only plugin sets Build permissions to allow
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
`sessions.json` holds session IDs and archive associations. `imports/` holds
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

Exit the active launcher before start/sync/reset/repair/terminal/purge. Status and stop remain
available in another terminal. Do not delete the lock file to bypass a live
process. For a failed import, retry the same flag with an updated launcher;
recovery uses the saved export without overwriting existing container work.
For a failed archive sync, use `sync`; the original live DB remains in its
volume. A deleted private volume cannot be recovered from session metadata alone.

### Maintenance shell environment

Use `oc-box terminal` to debug an existing container without launching OpenCode
or supplying host tokens. A missing command may need installing in that
container; terminal does not install baseline tools automatically. Its shell
deliberately skips user profiles, shell history, and normal credential forwarding.
A missing project marker or changed identity still requires explicit `repair`;
terminal cannot bypass those checks. If it reports an active launcher, exit
OpenCode and wait for its history sync/stop before retrying. Do not remove the
workspace lock file to bypass the running process.

## Installation and manuals

Requires Linux, Python 3.10 or later, rootless Podman with pasta networking,
delegated cgroup CPU/memory/PIDs controllers, and host OpenCode 1.x. Session
import/export compatibility was checked with OpenCode 1.19.5. Other major
OpenCode versions are rejected rather than assuming database/CLI compatibility.
Podman must work from the regular host user, not through sudo. Network access
is needed for initial distribution, runtime, and dependency downloads.

### Python package installations

Selected releases are also available through PyPI (after the first successful
publication). Choose one package-managed CLI installation, not system pip:

```sh
pipx install oc-box
# Or:
uv tool install oc-box
```

Inside an activated virtual environment, standard pip also works:

```sh
python -m pip install oc-box
```

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
