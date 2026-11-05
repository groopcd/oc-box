# oc-box

Copyright (C) 2026 Paulo Dias <paulo.miguel.dias@gmail.com>. Licensed under
**GPL-3.0-or-later**; see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Run **full, unmodified OpenCode inside a persistent rootless Podman container**.
MCPs, formatters, language servers, plugins, file tools, and shell commands all
use OpenCode's normal implementations in that same development environment.

The host launcher handles lifecycle and exports conversation history to your
normal host OpenCode database on exit. No tool-replacement plugin, OpenCode
fork, or edits to your host OpenCode configuration are needed.

## Install

Run from your ordinary host terminal as your regular user, without sudo:

```bash
python3 /home/groo/devel/oc-box/install.py
```

Requirements: Python 3.10+, rootless Podman with pasta networking, and OpenCode
1.x. Session import/export was tested with OpenCode 1.19.5. Installer destination:
`~/.local/share/oc-box`, launcher: `~/.local/bin/oc-box`. Ensure that bin directory
is on PATH. Start a new `oc-box` process after updates.

### First-milestone repository relocation

The source was relocated to `/home/groo/devel/oc-box`. If completing the initial
milestone from the original sandboxed editing session, quit that OpenCode
process first, then run this once from your regular host terminal:

```bash
python3 /home/groo/devel/oc-box/tools/finalize_milestone.py \
  --session ses_e539b6040ffeg5O1sOQugbB2N6 \
  --old-directory /home/groo/lixo
```

It backs up that conversation under `~/.local/state/oc-box/migrations`, creates
and commits this repository's first milestone, then associates the existing
session with the new Git project using supported OpenCode export/import. Other
conversations from the old directory are unaffected. It does not change Git
identity/configuration or push anything. Resume from the new directory with:

```bash
opencode /home/groo/devel/oc-box --session ses_e539b6040ffeg5O1sOQugbB2N6
```

## Start, exit, resume

Create a dedicated directory you own, enter it, and run:

```bash
oc-box
```

On Tumbleweed, the default image is:

```text
registry.opensuse.org/opensuse/tumbleweed:latest
```

The first launch downloads the image and installs Node/npm, Python/uv, Bash,
Git, ripgrep, curl, PDF text tools, and the same upstream OpenCode version as
your host. Project-specific dependencies can be installed by the AI inside the
container. Subsequent launches use the existing environment. Every launch checks
the exact host OpenCode version and installs that specific upstream npm release
inside the container if needed, including downgrades. The installed version is
verified before continuing; an unavailable or mismatched release stops startup
rather than selecting `latest` or keeping a different version. The TUI and
history commands use the verified installation's absolute executable path.

Startup prints named stages with elapsed times, with an update every five
seconds while a stage is waiting. Once the terminal is handed to OpenCode, a
temporary `Loading OpenCode, plugins and MCPs... Ns` line remains visible during
blank startup frames. It disappears when OpenCode first renders text. Terminal
input, queries, window resizing, and normal TUI output are relayed through a
PTY; no status writer runs over the rendered interface.

The first launch may take substantially longer for plugin/MCP downloads and
provider discovery. On a fresh environment, plugin dependency resolution runs
before the TUI opens, with its own timed `Resolving OpenCode plugin dependencies`
stage. The copied dependency manifest starts with the container's exact plugin
SDK version, retaining other declared dependencies; an incompatible host lockfile
is regenerated inside the container. This avoids starting from an older host SDK.
It does not eliminate genuine first-run downloads. Repeated launches skip that
extra resolution stage and compare the host dependency inputs against
the previous snapshot and retain the container's installed `node_modules` and
runtime-generated manifest/locks when those inputs and OpenCode's version are
unchanged. This avoids repeatedly downgrading an older host plugin-SDK manifest
and reinstalling the container's newer SDK on every launch. Changes to the host
dependency manifest, lockfiles, or runtime version invalidate that cache.
Configuration/plugins removed on the host are still removed from the copy.
This does not skip any enabled MCP, formatter, language server, or plugin.

Your project is mounted **at the same absolute path** inside the container.
For example, `/home/groo/test` remains `/home/groo/test`. This keeps configuration,
plugins, and LSP paths consistent. The container's HOME has the same pathname
as yours but is backed by a **private named volume**, not your host home.

On SELinux hosts, new project bind mounts use Podman's shared `:z` relabeling
option so the container can access project contents without disabling SELinux.
This changes labels only under the selected project. Startup checks that the
mount is readable/writable before importing or launching a session. If an older
container reports project `Permission denied`, run this from a host terminal:

```bash
chcon -R -t container_file_t /path/to/project
```

Then resume normally; no reset, purge, or repeated session import is needed.

Exiting OpenCode syncs conversation history, then stops the container. OS
packages remain in the stopped container; the private home/session database
remains in its named volume. Running processes need restarting next time.
Run `oc-box` again in the folder to resume the last root conversation. A second
active launcher for that workspace is rejected.

### Continue a host conversation inside the container

Exit the host OpenCode client first so the last turn is saved, then run:

```bash
oc-box --project /path/to/project --import-session ses_YOUR_HOST_SESSION_ID
```

For this repository's original conversation:

```bash
oc-box --project /home/groo/devel/oc-box \
  --import-session ses_e539b6040ffeg5O1sOQugbB2N6
```

This creates/starts the environment, exports the selected conversation from your
normal host database, imports it into the private container database, verifies
all exported messages/parts, and launches that exact session. The source session
must belong to the selected project directory. Files are already available via
the project bind mount, including `.git`, untracked files, and unfinished edits;
no separate project copy is needed.

The original host conversation remains untouched. A private recovery export is
saved in the workspace registry's `imports/` directory, outside the project.
Container-to-host history sync still creates its separate namespaced archive,
so later container progress does not overwrite your original host conversation.
Import transfers exported conversation/tool history, not running processes,
installed host packages, separate subagent transcripts, attached files outside
the project, or snapshot/undo storage. Reverted sessions that rely on host-only
snapshots cannot restore those snapshots inside the container.

The import is selected as the latest workspace conversation. On later visits,
just use `oc-box --project /path/to/project`. Do not repeat `--import-session`:
an existing session with the same ID is never overwritten or merged, protecting
work you have continued inside the container. `--import-session` is a start-only
option and cannot be combined with `--new` or `--resume`.

If import completed but verification failed or was interrupted, update the
launcher and retry the same `--import-session` command. When a recovery export
exists and the import has not been marked verified, oc-box compares the existing
container session against that original export without importing over it. If
the transcript matches, it records the successful recovery and resumes. If it
differs, it stops without overwriting either copy. Already verified imports are
still protected against repeated imports.

Verification checks every message/part and their contents. One mainline schema
normalization is accepted: stale tool `state.raw` buffers may be removed from
running, completed, or failed tool calls. `raw` belongs only to pending tool
states in mainline's import schema. Pending raw buffers, tool inputs/outputs,
metadata, text, and unrelated fields named `raw` remain strictly checked. The
private recovery export retains the original buffer as well.

To inspect a failed import from the host without printing conversation content:

```bash
python3 /home/groo/devel/oc-box/tools/diagnose_import.py \
  --workspace WORKSPACE_ID --session SESSION_ID
```

## MCPs, formatters, language servers, and plugins

Each launch snapshots `~/.config/opencode` (or its XDG equivalent) into the
private container home, including configured MCPs, local plugins, agents, skills,
and formatters/LSP configuration. This is a **copy**, not a writable host mount.
The global host configuration is not changed. The AI can also use project-level
configuration natively, since it executes inside the container.

Build mode in `oc-box` allows all OpenCode tool permissions, including shell
commands, file edits/reads throughout the container, MCP tools, and external
directories. A container-only profile and local plugin normalize Build's final
permission rules after loading inherited and project configuration, overriding
Build permission prompts and denials. Plan mode and subagents retain their
existing permission rules. Plain host OpenCode retains its usual permissions.
This changes OpenCode approvals, not Podman's mounts or isolation: the selected
host project remains fully writable, and the rest of the host filesystem is
not exposed. Copied plugins can still enforce their own checks independently.
Quit and restart OpenCode after updating the launcher to activate this profile.

* MCP servers run inside the container; remote MCPs connect normally.
* Node/npm and uv/uvx are provided for your Brave Search and DuckDuckGo MCPs.
* `BRAVE_API_KEY` and environment names ending in `_API_KEY` or `_TOKEN` are
  forwarded. Other required variables can be listed in `pass_env` below.
* Local MCPs without an explicit timeout get 120 seconds for first-run package
  downloads. Explicit timeouts remain unchanged.
* Native formatter/LSP support is enabled when the global config omits those
  fields. Explicit user settings, including `false`, are respected.
* Custom servers/binaries installed only on the host must also be installed
  in the container. Nothing can make a host-only executable available without
  copying/installing it. Built-in language servers retain their normal setup.
* Your host provider credentials and MCP auth files are copied when available;
  `OPENCODE_AUTH_CONTENT` is supported too. Credentials refreshed/logins made
  inside the container remain there; they are not written back to host auth.
* The existing sandbox plugin receives `OPENCODE_DISABLE_SANDBOX=1`. Bubblewrap
  does not wrap Bash inside this container. Other copied plugins still run.

HTTP endpoint URLs using `localhost`/`127.0.0.1` in your global config are mapped
to `host.containers.internal`, allowing the host Lemonade endpoint to work.
`LEMONADE_BASE_URL` is also supplied for the discovery plugin. `localhost` in
your project's own commands still means the container. Host services are
intentionally reachable over the network; their API capabilities still apply.

For additional environment variables, create `~/.config/oc-box/config.json`:

```json
{
  "pass_env": ["MY_MCP_SETTING", "MY_PROVIDER_SECRET"]
}
```

Host executable paths, shell startup files, SSH-agent/engine sockets, and host
proxy variables are not automatically forwarded.

## Session storage and history sync

The live database is in the container's private home volume. The host database
at `~/.local/share/opencode/` is **never mounted into the container**. On a normal
exit, oc-box uses mainline `export`/`import` commands to archive root-session
history into that normal host database. The archive has a workspace-specific
ID and a title starting with `[oc-box project-name]`; repeated syncs update that
archive copy. Resume with `oc-box` uses the original, full-fidelity container
session, including snapshots and other container-local data.

This is conversation-history synchronization, not live database replication:
host archives do not include snapshot stores, running processes, or separate
subagent transcripts. Conversations subsequently edited in plain host OpenCode
are not synchronized back into the container automatically. The one-time
`--import-session` option above brings an existing host conversation into a
workspace; it does not merge an ID that already exists in the container.

If the launcher is killed or sync fails, container data survives. Retry with:

```bash
oc-box sync
```

Each external sync command has a timeout. Failed sync does not purge the data.

## Distribution selection

```bash
oc-box distros
oc-box --distro tumbleweed
oc-box --distro leap
oc-box --distro ubuntu
oc-box --distro debian
oc-box --distro fedora
oc-box --distro arch
oc-box --distro alpine
```

Selection is saved per workspace. `opensuse`/`opensuse-tumbleweed` alias
`tumbleweed`; `opensuse-leap` and `archlinux` are accepted too. A Tumbleweed
image shares the host distro/userland, but may be a different rolling snapshot.
Ubuntu is pinned to 24.04; other tags are listed by `oc-box distros`.

Switch an existing environment explicitly:

```bash
oc-box reset --distro ubuntu
```

Reset rebuilds the OS and retains the project and private home/session volume.
System packages need reinstalling. Cached home tools with native dependencies
may also need reinstalling when switching distributions, especially Alpine.

### Devices and additional Podman settings

Add device mappings or additional Podman **create** options from the host
terminal when creating an environment:

```bash
oc-box --project /path/to/project --device /dev/accel/accel0
oc-box --project /path/to/project \
  --device /dev/dri/renderD128 \
  --podman-arg=--shm-size=1g
```

For an existing environment, exit its OpenCode client first and rebuild the
container with the new mapping:

```bash
oc-box reset --project /path/to/project --device /dev/accel/accel0
```

**Reset retains the session IDs, conversation history, private home, project
files, unfinished edits, and Git history.** It replaces the writable OS layer,
so system packages/files outside the private home and project need reinstalling;
running processes stop. The container's home caches remain available.

Both options are repeatable and saved in host-side workspace metadata. Later
launches and resets reuse them automatically. Repeating an identical setting
does not add duplicates; new settings append to the saved list. Remove or replace
saved settings explicitly:

```bash
oc-box reset --project /path/to/project --clear-devices
oc-box reset --project /path/to/project --clear-podman-args
oc-box reset --project /path/to/project --clear-devices --device /dev/accel/accel1
```

`--device` accepts Podman's `/host/device[:/container/device][:rwm]` syntax and
CDI names. `--podman-arg` takes **one literal long-option token per occurrence**,
including its value after `=`. It is not shell-parsed. For example:

```bash
oc-box reset --project /path/to/project \
  --device /dev/accel/accel0 \
  --podman-arg=--group-add=keep-groups \
  --podman-arg=--runtime=crun
```

The host user must have permission to access the device. `keep-groups` requires
the crun runtime, installed separately on the host. SELinux/device-driver rules
may also apply. Device passthrough does not install the driver-specific userland
SDK in the container; OpenCode can install those dependencies there.

#### Mapped device reports `Permission denied`

A visible device name with `-?????????` in `ls -l` means the path is mapped but
reading its attributes is denied. In rootless Podman, device bind mounts retain
the host device's SELinux label. On an enforcing host, check the device policy
and host permissions from a **host terminal**, as your regular user:

```bash
ls -ldZ /dev/accel
ls -lZ /dev/accel/accel0
id
/usr/sbin/getenforce
/usr/sbin/getsebool container_use_devices
test -r /dev/accel/accel0 && test -w /dev/accel/accel0
```

On openSUSE the SELinux utilities are under `/usr/sbin`, which may not be on a
regular user's PATH. Reading these settings does not require sudo. If the host
user can access the device but SELinux is enforcing with
`container_use_devices --> off`, Podman's documented device-policy setting is:

```bash
sudo /usr/sbin/setsebool -P container_use_devices on
```

This persistent **host-wide SELinux policy setting** allows containers to use
device labels, subject to their existing mappings and Unix permissions. It
does not map additional devices or disable SELinux. It applies immediately to
already running containers: no reset, reboot, reinstall, or session import is
needed. Retry inside the existing container:

```bash
ls -l /dev/accel/accel0
prlimit --pid $$ --memlock
xrt-smi examine
```

If the device becomes stat-able but access still fails, check its host ACL and
supplementary-group access separately. Membership of the host `render` group
does not automatically carry into a rootless container; use the crun and
`--group-add=keep-groups` settings above when that group access is required.
Container group names alone do not preserve host group permissions.

A memlock limit does not cause `ls` to fail reading device attributes. In a
command joined with `&&`, a failing `ls` prevents the memlock check and hardware
utility from running at all; diagnose their output after device access works.

See [Podman's device documentation](https://docs.podman.io/en/latest/markdown/podman-run.1.html#device-host-device-container-device-permissions).

oc-box reserves its managed name/ownership labels, user, workdir, entrypoint,
and lifecycle options, and has dedicated flags for CPU/RAM/process limits. Other
Podman options are passed through before the image name. Settings that expose
more host resources (devices, extra mounts, networking, capabilities, security
options) change the container's boundary as explicitly requested by that host
command; they are not automatically requested by the AI. Extra bind-mounted host
files and externally supplied volumes are not deleted by `purge` unless they
are explicitly labeled as owned by that workspace. `--status` shows the saved
device mappings and additional Podman options.

## Lifecycle commands

```bash
oc-box                         # Create or resume
oc-box --resume                # Explicit resume (requires an existing workspace)
oc-box --new                   # New conversation, same environment
oc-box --import-session ID     # Import a host conversation and continue it here
oc-box --model provider/model  # Select model
oc-box --status                # Resource snapshot for all managed workspaces
oc-box status                  # Alias for --status
oc-box --status --project PATH # Resource snapshot for one project
oc-box --status --json         # Machine-readable metrics and storage in bytes
oc-box --about                 # Version, author, AI contribution, and license
oc-box --version               # Commit-derived oc-box version
oc-box list                    # All workspaces and latest recorded session IDs
oc-box stop                    # Stop, including from another terminal
oc-box sync                    # Retry history sync without opening the TUI
oc-box reset                   # Rebuild OS, keep home volume/project/history
oc-box purge                   # Remove sandbox/home volume and oc-box metadata
oc-box purge --delete-sessions # Also delete host archive conversations
oc-box purge --all             # Also DELETE THE PROJECT DIRECTORY
```

`oc-box list` prints a header and space-aligned columns: `WORKSPACE ID`, `DISTRO`,
`PROJECT`, `STATUS`, and `SESSION ID`. Column widths expand to fit complete IDs
and paths. `STATUS` indicates whether the project directory is present, not
whether its container is running. `SESSION ID` shows the latest recorded
container OpenCode session, or `none` if no session has been recorded.
This is the original session used for resume, not the host archive's separate
ID. The listing reads local metadata without starting containers; its session
ID is refreshed at startup and history sync, so a newly created conversation in
an active session may not appear until the next sync or launch. Missing project
directories are still listed.

### Resource status

Run the status command from a host terminal as your regular user:

```bash
oc-box --status
oc-box --status --workspace WORKSPACE_ID
oc-box --status --project /path/to/project
oc-box --status --json
```

Without a selector, both `--status` and `status` report all registered
workspaces. A project/workspace selector limits the snapshot to one environment.
The report includes:

* State, sampled CPU percentage, RAM usage/limit, process count, and uptime.
* Configured CPU/process limits, image, storage driver, last exit code,
  out-of-memory state, and health status when present.
* Separate disk sizes for the writable container OS layer, private home volume,
  and project directory; the private home contains session history and caches.
* Filesystem used/free space, block-I/O counters, and the latest recorded
  container session and synced host archive IDs.

It does not start, stop, or execute commands inside managed containers, and it
can run while OpenCode is active. Stopped containers retain storage information
but have no live CPU/RAM sample. Missing resources or unavailable metrics are
shown explicitly rather than as zero usage. `--json` emits a single JSON object
with numeric byte values, `null` for unavailable measurements, and per-workspace
notes; progress is sent only to stderr.

CPU is a short snapshot, with 100% representing one CPU core. Rootless pasta
network counters are omitted because Podman cannot reliably report them. Disk
scans use allocated blocks, do not follow symlinks or cross filesystems, and are
bounded to 15 seconds per directory. Private-volume scans enter the rootless
user namespace to read container-owned files without needing sudo. They can take
longer for very large projects or caches. Writable OS size excludes shared base
image layers; rootfs-with-image size is shown separately and should not be added
across workspaces as unique storage. Btrfs compression, reflinks, snapshots, and
shared blocks mean these sizes are estimates, not exclusive physical disk use.

### About and automatic versions

```bash
oc-box --about
oc-box --version
```

`--about` credits Paulo Dias <paulo.miguel.dias@gmail.com> as the author and
acknowledges implementation, debugging, test, and documentation assistance from
OpenAI's coding assistant through OpenCode. It includes the license and source
commit, and works without Podman or a registered workspace.

The version format is `0.1.N+gHASH`, where `N` is `git rev-list --count HEAD` and
`HASH` identifies the source commit. Each new commit automatically advances the
count on that history; no version-bump commit or Git hook is needed. Tracked
uncommitted changes add `.dirty`; untracked sandbox placeholders do not. Keep
the full Git history for accurate counts. The installer records the current
version/commit in an installed `build-info.json`, so installed copies report
their own build even without Git. Reinstall after new commits to update that
installed version. This is oc-box's version, independent of the exact host
OpenCode version matched inside containers.

### Purge and workspace identity

Purge asks for the first eight characters of its workspace ID; `--yes` skips
confirmation. `--project PATH` works from another directory. After deleting a
project manually, find its ID using `oc-box list`, then run:

```bash
oc-box purge --workspace WORKSPACE_ID
```

Default purge attempts a final sync while the project exists. If you already
deleted the directory, it retains the last host archive and removes container
storage without another sync. Purge removes only this workspace's labeled
containers, volumes, and networks. Shared base-image caches remain.

Workspace IDs, canonical paths, and directory device/inode identity are recorded
outside the project. `.oc-box-id` identifies the folder; copied markers cannot
authorize another mount. Deleting and recreating the folder starts a new
workspace. Remove an accidentally copied marker before starting a new one.

## Resource limits and boundary

Defaults: 4 CPUs, 8 GiB RAM, 2048 processes. Override at creation/reset:

```bash
oc-box --cpus 8 --memory 16g --pids-limit 4096
oc-box reset --cpus 8 --memory 16g
```

### openSUSE: enable rootless resource-controller delegation

Some openSUSE installations ship
`/usr/lib/systemd/system/user@.service.d/20-defaults-SUSE.conf` with an empty
`Delegate=` setting. The user manager may then have only `pids` delegated even
though the kernel supports memory and CPU controllers. With limits enabled,
container startup can fail with `memory.swap.max: no such file or directory`
and the misleading trailing message `OCI runtime attempted to invoke a command
that was not found`. This does not mean an executable is missing from the image.

oc-box checks effective delegation before creating/starting its environment and
retains the configured resource limits. To repair delegation for your own user,
run from your ordinary host terminal (substitute your numeric UID if not 1000):

```bash
sudo systemctl edit user@1000.service --drop-in=90-oc-box-delegate.conf
```

In the editable override area, save:

```ini
[Service]
Delegate=cpu cpuset io memory pids
```

The `90-` ordering overrides the vendor's `20-` defaults. Then run
`sudo systemctl daemon-reload` and reboot so the user manager is recreated with
the new delegation. From the new login, the following file should include
`cpu memory pids` (it may list additional controllers):

```text
/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/cgroup.controllers
```

Retry the same `oc-box --project /path/to/project` command. The failed container,
downloaded image, private home volume, and project can be reused; no purge or
reset is needed. Do not edit the vendor file under `/usr/lib`.

The agent can change its entire container OS/private home and everything in
the mounted project. The host home/database/oc-box registry are not mounted.
By default, rootless Podman uses normal namespace/seccomp controls without
privileged mode, engine sockets, host devices, or host PID/network namespace
sharing. Explicit `--device`/`--podman-arg` settings can extend those defaults.

Containers share the host kernel. Network access includes configured host APIs
and LAN services. There is no disk quota in this version: project and image/
volume growth can fill the host filesystem. Resource isolation is not a VM.

## License and credits

The original oc-box code and documentation are free software under the GNU
General Public License, version 3 or (at your option) any later version
(`GPL-3.0-or-later`). Copyright (C) 2026 Paulo Dias. The full license and
attribution notice are included in source and installed copies. There is no
warranty. OpenCode, Podman, distribution images, MCPs, and other separately
installed tools retain their respective copyrights and licenses; oc-box does
not relicense or modify them.

## Verification

```bash
python3 -B -m unittest discover -s /home/groo/devel/oc-box/tests -v
python3 -B /home/groo/devel/oc-box/tests/smoke_mainline.py
```

The mainline smoke test has bounded subprocess timeouts and an offline fake
model/MCP/LSP/formatter. It verifies real OpenCode file tools, automatic
formatting, LSP startup/document notifications, MCP invocation, session
export/import, and resume with temporary app data. Host-to-container import is
also checked with a multi-megabyte transcript, duplicate protection, and exact
imported-session resume. It does **not** substitute for a live Podman check.
OpenCode's current outer Bubblewrap sandbox prevents
that check in this editing session.

From a normal host terminal, launch a fresh dedicated test folder with `oc-box`
and ask it to read `/etc/os-release`, test your MCPs, edit a supported source
file, then exit/relaunch to check resume. Keep your regular-user shell; do not
run `oc-box` under sudo.
