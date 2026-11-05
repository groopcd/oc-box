# oc-box

**Persistent rootless Podman development environments for unmodified OpenCode.**

Run OpenCode, MCP servers, formatters, language servers, plugins, and development
tools together inside a container. Give the agent control of that environment
while exposing only your selected project directory by default.

Copyright (C) 2026 Paulo Dias <paulo.miguel.dias@gmail.com>.
Licensed [GPL-3.0-or-later](LICENSE); see [NOTICE](NOTICE) for attribution and
the acknowledgment of OpenAI coding-assistant contributions through OpenCode.

## Why oc-box?

oc-box grew out of wanting OpenCode to have more freedom without giving it
unrestricted access to the host. A Bubblewrap-based host sandbox is useful for
restricting commands, but installing system packages and experimenting with
services can mean working around host restrictions. **oc-box gives OpenCode its
own persistent Linux development environment**, where it can install tools,
change system configuration, run services, and experiment.

The practical advantages for that workflow are:

- **Install what the project needs:** compilers, SDKs, databases, and distro
  packages live inside the container rather than changing the host system.
- **Keep integrations working:** OpenCode itself, MCP servers, formatters,
  language servers, and plugins run together inside the same environment.
- **Choose the project's distro:** Tumbleweed, Leap, Ubuntu, Debian, Fedora,
  Arch, or Alpine, with dependencies specific to that project.
- **Pick up where you left off:** installed tools, project files, and
  conversations persist. Run `oc-box` again to resume, or import an existing
  host OpenCode conversation and continue it inside the container.
- **Recover a broken environment:** rebuild the container OS while retaining
  your project, private home, conversation history, and session IDs.
- **Control resources and hardware:** set CPU/RAM/process ceilings and
  explicitly pass through GPU/NPU devices when the project needs them.

OpenCode stays on mainline without patches, and the container matches the
host's installed OpenCode version. The goal is a more capable, replaceable
development environment. Rootless containers still share the host kernel, and
the mounted project remains fully writable; see [SECURITY.md](SECURITY.md) for
the boundary and tradeoffs.

## Requirements and installation

- Linux, Python 3.10+, rootless Podman with pasta networking.
- Delegated CPU, memory, and process cgroup controllers.
- A host OpenCode 1.x installation. Integration checks use OpenCode 1.19.5.
- Network access for initial image, package, and plugin downloads.

From a source checkout, run on the **host as your regular user**, without sudo:

```sh
git clone https://github.com/groopcd/oc-box.git
cd oc-box
python3 install.py
oc-box --version
```

The installer copies the launcher to `~/.local/share/oc-box` and creates
`~/.local/bin/oc-box`. Add that bin directory to PATH if needed. No Python/npm
dependencies or documentation build tools are required for installation.
Reinstall after source updates; restart OpenCode to load new configuration.

## Quick start

```sh
mkdir -p ~/devel/demo
oc-box --project ~/devel/demo
```

On Tumbleweed the default image is
`registry.opensuse.org/opensuse/tumbleweed:latest`. Initial setup installs
Node/npm, Python/uv, Bash, Git, curl, ripgrep, PDF tools, and the **exact host
OpenCode version**. Named startup stages show progress. Subsequent launches
reuse the container, dependency caches, files, and last saved conversation.

Your project is mounted writable at the same absolute path. A private named
volume provides the container home: it has your home pathname but is **not your
host home**. You can install more system packages inside the container.

Exit OpenCode normally to synchronize conversation archives to the host and
stop the environment. Running the same command resumes the original container
session; stopped processes must be restarted.

## Common commands

```sh
oc-box                              # Create/resume in the current directory
oc-box --new                        # New conversation, same environment
oc-box list                         # Workspace table and latest session IDs
oc-box --status                     # CPU, RAM, storage, state, limits, sessions
oc-box --status --project ~/devel/demo --json
oc-box distros                      # Supported images and host default
oc-box stop --project ~/devel/demo
oc-box sync --project ~/devel/demo   # Retry host history sync after a crash
oc-box repair --project ~/devel/demo # Accept a verified directory identity change
oc-box reset --project ~/devel/demo --cpus 8 --memory 16g
oc-box purge --project ~/devel/demo  # Remove environment; retain source files
oc-box --about
```

`--workspace ID` selects a registered workspace from any directory. It also
allows cleanup after a project directory was deleted. `--status` does not start
or stop containers and can run while OpenCode is active.

### Persistence and deletion

| Operation | Project and unfinished edits | Private home/session IDs | Container OS |
| --- | --- | --- | --- |
| Exit or `stop` | Kept | Kept | Kept, stopped |
| `sync` | Kept | Kept; host archive refreshed | Kept, stopped |
| `reset` | Kept | Kept | Rebuilt |
| `purge` | Kept | Deleted | Deleted |
| `purge --all` | **Deleted** | **Deleted** | Deleted |

Reset removes system packages and files outside the project/private home.
Home-installed dependencies, Git history, and session IDs remain. CPU/RAM/process
limits and device mappings are create-time settings in oc-box; changing them for
an existing container requires reset, not purge. CPU and RAM limits are ceilings,
not reservations. Defaults are 4 CPU cores, 8 GiB RAM, and 2048 processes.

Purge asks for an ID-prefix confirmation. `--delete-sessions` additionally
deletes recorded host archive copies; `--all` includes that action and deletes
the project directory. Shared image caches and unrelated resources remain.

### Directory recovery and duplicate detection

Clearing host `~/.cache` does not delete workspace registrations or private
session volumes. If directory device/inode changes, keep the marker and use
`oc-box repair` after checking the project and stopping its launcher. If the
marker is missing, the launcher detects existing registrations instead of
silently creating a duplicate:

```sh
oc-box --status
oc-box repair --workspace WORKSPACE_ID
oc-box --workspace WORKSPACE_ID --resume
```

Repair restores a missing marker or accepts the current identity while retaining
the container, settings, and session IDs. It never replaces another workspace's
marker. If several records reference the same path, select the intended ID.
`list`/`status` distinguish identity changes, missing markers, and marker
mismatches, with specific recovery or cleanup guidance.

For a stale duplicate left by an older launcher, first recover any needed work,
then run **plain** `oc-box purge --workspace STALE_ID`, without `--all` or
`--delete-sessions`. It leaves the current project/marker and host histories
intact and retains private archive provenance under the state directory's
`retired/` folder. This is saved recovery data, not a third active workspace.

### Continue an existing host session

Exit the host OpenCode client first, then import once:

```sh
oc-box --project ~/devel/demo --import-session ses_HOST_SESSION_ID
```

The source session must belong to that project. oc-box saves a private recovery
export, verifies the imported transcript, and resumes the same session ID.
Your original host conversation is unchanged. Later launches need no import
flag. See [session recovery](docs/cli.md#sessions-and-recovery) for retry behavior.
The same flag can recover an oc-box host archive into a fresh environment after
its marker was replaced; keep the original registry and saved history export.
After plain purge with a current launcher, the retained private `retired/`
provenance can verify the old host archive instead. Older already-purged records
cannot be reconstructed automatically; the imported live session is unaffected.

### Select a distro or map hardware

```sh
oc-box --project ~/devel/demo --distro ubuntu
oc-box reset --project ~/devel/demo --distro fedora
oc-box reset --project ~/devel/demo --device /dev/accel/accel0
oc-box reset --project ~/devel/demo --podman-arg=--shm-size=1g
```

Choices: Tumbleweed, Leap, Ubuntu, Debian, Fedora, Arch, and Alpine. Distro and
device settings persist per workspace. Repeat `--device`/`--podman-arg` for
multiple settings; clear old values with `--clear-devices`/`--clear-podman-args`.
Podman arguments are literal long-option tokens, not shell snippets.

Rootless device access may require crun with `--group-add=keep-groups`, host
ACLs, and SELinux's `container_use_devices` setting. oc-box checks these
prerequisites and prints advisory warnings before device-enabled creation.
See [device troubleshooting](docs/cli.md#devices-and-host-permissions) for exact
host commands; oc-box never changes host policy automatically.

## Integrations and permissions

Global OpenCode configuration, skills, agents, plugins, MCP settings, and auth
are copied into private storage at startup. MCPs, formatters, and language
servers run natively inside the container. Host-only executables must be
installed there too. Changes to host configuration apply on the next launch.

Build mode allows all OpenCode tool permissions inside oc-box; Plan and
subagent rules are unchanged. The existing Bubblewrap plugin is disabled for
the container launch. HTTP service endpoints using localhost in global config
are mapped to `host.containers.internal`, including the host Lemonade endpoint.
Plain host OpenCode retains its normal configuration and permissions.

API-key/token environment variables are forwarded, and additional names can be
listed in `~/.config/oc-box/config.json` under `pass_env`. Credentials remain
available to the container's processes and copied config/auth may contain
secrets. Only forward credentials you intend the agent to use. Forwarded secret
values are not embedded in Podman's command-line arguments.

Read [SECURITY.md](SECURITY.md) for the trust boundary and reporting guidance.
Rootless containers share the host kernel, have network access, and do not have
a disk quota. Snapshots/archives are not backups against disk or volume loss.

## Documentation

The [complete CLI reference](docs/cli.md) covers **every command and option**,
including defaults, examples, persistence, crash recovery, expected failures,
configuration, resource metrics, and troubleshooting.

The installer includes prebuilt man and GNU Info pages:

```sh
man oc-box
info oc-box
```

If local documentation directories are not searched automatically:

```sh
man "$HOME/.local/share/man/man1/oc-box.1"
info --file "$HOME/.local/share/info/oc-box.info"
```

Readers are host packages (`man` and `info` on openSUSE). See
[CONTRIBUTING.md](CONTRIBUTING.md) to regenerate the manuals from `docs/cli.md`.
`oc-box --version` uses `0.1.N+gHASH`, advancing automatically with Git commits;
installed copies retain their source version until reinstalled.

## Development status

The project is under active development. Tumbleweed host/container launch,
warm resume, host-session import, MCP connection, and mapped NPU access have
been exercised. The automated mainline test uses temporary databases and local
model/MCP/LSP/formatter fixtures; it does not prove Podman isolation or certify
every distro/runtime/device combination.

```sh
python3 -B -m unittest discover -s tests -v
python3 -B tests/smoke_mainline.py
python3 -B tools/build_docs.py --check
```

Other invoked programs retain their own licenses. oc-box is provided without
warranty under the GPL; see [LICENSE](LICENSE) and [NOTICE](NOTICE).
