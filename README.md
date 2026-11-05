# oc-box

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

## MCPs, formatters, language servers, and plugins

Each launch snapshots `~/.config/opencode` (or its XDG equivalent) into the
private container home, including configured MCPs, local plugins, agents, skills,
and formatters/LSP configuration. This is a **copy**, not a writable host mount.
The global host configuration is not changed. The AI can also use project-level
configuration natively, since it executes inside the container.

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

## Lifecycle commands

```bash
oc-box                         # Create or resume
oc-box --resume                # Explicit resume (requires an existing workspace)
oc-box --new                   # New conversation, same environment
oc-box --import-session ID     # Import a host conversation and continue it here
oc-box --model provider/model  # Select model
oc-box status                  # Workspace/container and recorded session
oc-box list                    # All workspaces and latest recorded session IDs
oc-box stop                    # Stop, including from another terminal
oc-box sync                    # Retry history sync without opening the TUI
oc-box reset                   # Rebuild OS, keep home volume/project/history
oc-box purge                   # Remove sandbox/home volume and oc-box metadata
oc-box purge --delete-sessions # Also delete host archive conversations
oc-box purge --all             # Also DELETE THE PROJECT DIRECTORY
```

`oc-box list` includes `session=ses_...` for each workspace's latest recorded
container OpenCode session, or `session=none` if no session has been recorded.
This is the original session used for resume, not the host archive's separate
ID. The listing reads local metadata without starting containers; its session
ID is refreshed at startup and history sync, so a newly created conversation in
an active session may not appear until the next sync or launch. Missing project
directories are still listed.

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
Rootless Podman uses normal namespace/seccomp controls without privileged mode,
engine socket, host devices, or host PID/network namespace sharing.

Containers share the host kernel. Network access includes configured host APIs
and LAN services. There is no disk quota in this version: project and image/
volume growth can fill the host filesystem. Resource isolation is not a VM.

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
