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
container. Subsequent launches use the existing environment. Updating host
OpenCode causes the container version to be updated on its next launch.

Your project is mounted **at the same absolute path** inside the container.
For example, `/home/groo/test` remains `/home/groo/test`. This keeps configuration,
plugins, and LSP paths consistent. The container's HOME has the same pathname
as yours but is backed by a **private named volume**, not your host home.

Exiting OpenCode syncs conversation history, then stops the container. OS
packages remain in the stopped container; the private home/session database
remains in its named volume. Running processes need restarting next time.
Run `oc-box` again in the folder to resume the last root conversation. A second
active launcher for that workspace is rejected.

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
are not synchronized back into the container.

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
oc-box --model provider/model  # Select model
oc-box status                  # Workspace/container and recorded session
oc-box list                    # All workspaces, including deleted directories
oc-box stop                    # Stop, including from another terminal
oc-box sync                    # Retry history sync without opening the TUI
oc-box reset                   # Rebuild OS, keep home volume/project/history
oc-box purge                   # Remove sandbox/home volume and oc-box metadata
oc-box purge --delete-sessions # Also delete host archive conversations
oc-box purge --all             # Also DELETE THE PROJECT DIRECTORY
```

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
export/import, and resume with temporary app data. It does **not** substitute
for a live Podman check. OpenCode's current outer Bubblewrap sandbox prevents
that check in this editing session.

From a normal host terminal, launch a fresh dedicated test folder with `oc-box`
and ask it to read `/etc/os-release`, test your MCPs, edit a supported source
file, then exit/relaunch to check resume. Keep your regular-user shell; do not
run `oc-box` under sudo.
