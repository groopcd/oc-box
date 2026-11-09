# oc-box

**Persistent rootless Podman development environments for unmodified OpenCode.**

Run OpenCode, MCP servers, formatters, plugins, and development tools together
inside a container, with native language servers on OpenCode 1.x. Give the agent
control of that environment while exposing only your selected project directory
by default.

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
  and plugins run together inside the same environment. OpenCode 1.x also runs
  native language servers; upstream 2.2.6 does not provide native LSP.
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
- A host OpenCode 1.x or 2.x installation (2.x support requires oc-box 1.5+).
  Integration checks use 1.19.5 and 2.2.6; see [2.x compatibility](docs/opencode-2.md).
- Git on the host for installation from GitHub and self-updates; curl for the
  one-line installer.
- Network access for initial image, package, and plugin downloads.

Check installed tools and get feature-specific installation commands:

```sh
oc-box doctor
oc-box doctor --feature backup --feature vault
oc-box doctor --feature backup-info --json
```

Missing optional dependencies identify the affected feature without disabling
unrelated commands. A configured V2 vault requires crypto support and fails
closed if it is missing; without a vault, env-file compatibility launches remain
available. Native OpenCode saved/OAuth credential refresh and copying remain
independent in every mode. Missing age on Tumbleweed suggests
`sudo zypper refresh && sudo zypper install age`; Ubuntu/Debian use apt, Fedora
dnf, Arch pacman, and Alpine apk. Commands are for the OS running doctor, not
the workspace image. See the [host dependency table](docs/cli.md#host-dependency-installation)
for all seven distros and the distinction from automatically installed workspace
tools. Doctor does not install packages, unlock secrets, start Podman, or change
workspaces. Inside oc-box it reports container prerequisites only; run from the
host terminal for host advice.

Install on the **host as your regular user**, without sudo. Choose one channel:

| Method | Install | Update |
| --- | --- | --- |
| **pipx — isolated CLI from [PyPI](https://pypi.org/project/oc-box/)** | `pipx install oc-box` | `pipx upgrade oc-box` |
| **uv — isolated CLI from PyPI** | `uv tool install oc-box` | `uv tool upgrade oc-box` |
| **pip — inside an activated virtual environment** | `python -m pip install oc-box` | `python -m pip install --upgrade oc-box` |
| **GitHub — latest development branch** | One-line installer below | `oc-box upgrade` |

### Install from PyPI

For a standalone command, pipx keeps oc-box in its own Python environment:

```sh
pipx install oc-box
pipx ensurepath
oc-box --version
```

For the recommended V2 global vault workflow from your **first project launch**,
install `pipx install 'oc-box[vault]'` or `uv tool install 'oc-box[vault]'` instead.
The GitHub installer accepts `sh -s -- --with-vault`. Then initialize/populate the
vault before launching the project, as shown below; no per-project registration
is required. Standard installs do not automatically install the extra.

Alternatively, use uv's tool manager:

```sh
uv tool install oc-box
uv tool update-shell
oc-box --version
```

Open a new terminal if the tool manager has just added its bin directory to
PATH. Both methods install published releases from
**[pypi.org/project/oc-box](https://pypi.org/project/oc-box/)**.

For standard pip, create and activate a dedicated virtual environment first:

```sh
python3 -m venv ~/.venvs/oc-box
. ~/.venvs/oc-box/bin/activate
python -m pip install oc-box
oc-box --version
```

Use the activated environment or `~/.venvs/oc-box/bin/oc-box` on later visits.
Podman, host OpenCode, and Linux setup remain external requirements. There is
no need for `sudo pip` or a system/user-site pip installation.

### Install from GitHub or source

The existing one-line installer remains available for the latest GitHub code:

```sh
curl -fsSL --proto '=https' --tlsv1.2 https://raw.githubusercontent.com/groopcd/oc-box/master/install.sh | sh
```

This downloads and runs the public installer; it trusts the published GitHub
source. To inspect it first, download `install.sh`, review it, and run `sh
install.sh`. Git clones the full public branch history anonymously into a
temporary directory so installed versions retain their accurate commit count.
It does not use or save your GitHub/provider tokens.

Alternatively, install from a source checkout:

```sh
git clone https://github.com/groopcd/oc-box.git
cd oc-box
python3 install.py
oc-box --version
```

### Upgrade PyPI installations

The PyPI and GitHub channels use the same code and workspace format. Choose
the package manager that owns your CLI installation when upgrading it:

```sh
pipx upgrade oc-box
uv tool upgrade oc-box
# Inside the installation's virtual environment:
python -m pip install --upgrade oc-box
```

`oc-box upgrade` detects Python-package installations and prints the appropriate
command; it does not overwrite package-managed files or create a competing
GitHub-installed launcher. GitHub/curl installations retain `oc-box upgrade` and
opt-in automatic GitHub updates. If both channels are installed, check
`command -v oc-box` to select the intended launcher. PyPI package versions use
`1.6.N`, while `oc-box --version` also records `+gHASH` for source provenance.
Published PyPI releases are selected separately from ordinary GitHub commits.
Packaged manuals are in the package's `docs/` and the tool environment's
`share/man`/`share/info`; those directories may need explicit reader paths.

### OpenCode 2.x shared clients

For plain host `opencode --standalone` outside oc-box, the original V1 plugins
are not automatically adapted. The opt-in [host plugin migration utility](docs/host-plugins.md)
audits and ports known plugins to direct V2 implementations in your normal host
configuration, retiring obsolete V1 workarounds. A separate compatibility profile
is still optional. These are source-checkout tools, not installed oc-box commands;
real host sandbox enforcement requires its documented acceptance checks.
Plain host `opencode --standalone` uses the host's normal environment/auth and
does not access the oc-box vault. It is not an oc-box flag; oc-box continues to
own a private foreground V2 service per active workspace.

While a V2 workspace is running, open its same session from another host terminal:

```sh
oc-box web --project ~/devel/demo
oc-box terminal --project ~/devel/demo
```

The web UI and session shell terminal reuse the existing service/credentials
without ending the TUI session. Web access is host-loopback-only with a native
one-use pairing link; keep its foreground command open. The owner waits for
attached clients before final sync/stop. `Ctrl-]` detaches from the session shell,
preserving it for reattachment; `exit` closes that shell. `terminal --maintenance`
retains the restricted shell, and 1.x retains its previous terminal behavior. See the
[V2 client guide](docs/opencode-2.md#concurrent-web-terminal-and-full-tui-clients).

### Bash completion

Enable command and option completion in your current Bash shell:

```bash
source <(oc-box completion bash)
```

Add that line to `~/.bashrc` after your PATH setup to enable future shells;
this works with uv, pipx, and GitHub installations. GitHub/source installs also
install an autoload file under `~/.local/share/bash-completion/completions/oc-box`
for hosts with the bash-completion framework enabled. Python packages ship the
same script in their private environment. See the [completion reference](docs/cli.md#bash-completion)
for autoload setup and paths. Completion includes command-specific options,
distros, doctor features, and file/directory paths; it never queries Podman or
unlocks credentials.

### GitHub installation layout

The GitHub/curl installer copies the launcher to `~/.local/share/oc-box` and creates
`~/.local/bin/oc-box`. Add that bin directory to PATH if needed. No Python/npm
dependencies or documentation build tools are required for installation.
Restart OpenCode to load new configuration. The installer stages a complete
versioned copy under `~/.local/share/oc-box/releases/`, then atomically switches
the launcher. Existing sessions keep their original code until they exit;
earlier installed copies are retained, not removed while they may be in use.
For the first migration from an older flat installation, exit its active oc-box
launchers before reinstalling. The pinned-release guarantee applies afterward.

### Upgrade from GitHub

```sh
oc-box upgrade --check       # Check for a changed published version
oc-box upgrade               # Install the current public GitHub master commit
oc-box upgrade --auto-update on
oc-box upgrade --auto-update off
```

Automatic updates are **opt-in**. When enabled, normal installed `oc-box` starts
check GitHub before opening a workspace, install a changed version, and continue
with the same arguments. Network/update failures print a message and continue
with the installed version. There is no background daemon or update during an
active session. Informational commands, maintenance terminals, reset/purge, and
direct source-checkout launches do not perform automatic checks.

Upgrades replace only the installed launcher/modules/manuals. They do not pull
into or change a source checkout, reset containers, delete workspaces, touch
provider credentials, or change the host OpenCode version. A locally installed
commit ahead of the published branch is retained. No GitHub token is required;
updates use the fixed public HTTPS upstream without global Git config, helpers,
hooks, or forwarded environment secrets. This follows the development branch,
not signed release artifacts; see [SECURITY.md](SECURITY.md).

## Quick start

For OpenCode 2.x, install vault support using the extra above (or curl
`--with-vault`), then store credentials on the host **before the first launch**:

```sh
oc-box secrets init
oc-box secrets set ANTHROPIC_API_KEY
oc-box secrets set MCP_API_KEY
mkdir -p ~/devel/demo
oc-box --project ~/devel/demo
```

Values/passphrases are entered only in masked host-terminal prompts. V2 without
an explicit workspace credential policy uses **all current global vault entries**
on each new owning start/reset/import/web; no project registration or selection
is needed. Use provider/MCP `{env:NAME}` references, never literal secret values
in JSON; see the [published V2 configuration examples](docs/opencode-2.md#provider-and-mcp-references-not-json-secrets).
Without a vault, the same project launch retains env-file fallback and offers
concise setup guidance, without automatic installation
or config/JSON-secret import. An initialized empty vault unlocks and delivers no
entries. V1's legacy env-file default is unchanged. Native OpenCode saved/OAuth
credential refresh/copy remains independent in every credential mode, including
global vault mode. Explicit workspace env/selected policies remain overrides.

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
oc-box terminal --project ~/devel/demo # V2 session shell; V1 maintenance shell
oc-box terminal --maintenance --project ~/devel/demo # Clean maintenance shell
oc-box sync --project ~/devel/demo   # Retry host history sync after a crash
oc-box repair --project ~/devel/demo # Accept a verified directory identity change
oc-box reset --project ~/devel/demo --cpus 8 --memory 16g
oc-box start --project ~/devel/demo --no-vault # One owning V2 compatibility launch
oc-box purge --project ~/devel/demo  # Remove environment; retain source files
oc-box --about
oc-box upgrade --check
```

`--workspace ID` selects a registered workspace from any directory. It also
allows cleanup after a project directory was deleted. `--status` does not start
or stop containers and can run while OpenCode is active.

### Persistence and deletion

| Operation | Project and unfinished edits | Private home/session IDs | Container OS |
| --- | --- | --- | --- |
| Exit or `stop` | Kept | Kept | Kept, stopped |
| `sync` | Kept | Kept; host archive refreshed | Kept, stopped |
| `terminal` (V1/maintenance) | Kept | Kept; no history sync | Kept, prior run state restored |
| `terminal` (V2 attach) | Kept | Shared current session | Kept; owner controls stop |
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

### Maintenance and debugging

After exiting the workspace's OpenCode client, enter its existing container:

```sh
oc-box terminal --maintenance --project ~/devel/demo
# Or, from another directory:
oc-box terminal --maintenance --workspace WORKSPACE_ID
```

This opens a clean interactive shell at the project path, with the same mounts,
devices, limits, and rootless permissions. Type `exit` or Ctrl-D to leave. A
container started by this command is stopped on exit; one already running is
left running. No OpenCode launch, configuration refresh, dependency install,
session import/sync, or environment recreation occurs.

Host tokens, API keys, custom `pass_env`, proxies, and agent variables are not
forwarded. The shell's environment is cleared, startup files are skipped, and
history persistence is disabled. Credentials already stored in the container's
files or held by its other processes remain accessible: a maintenance shell is
not isolation from the existing container. It requires a real terminal and the
workspace's exclusive lock, so exit an active OpenCode launcher first.

### Directory recovery and duplicate detection

Clearing host `~/.cache` does not delete workspace registrations or private
session volumes. Workspace identity uses **generic Linux VFS interfaces**, not
filesystem-specific tools or branches: the canonical path and marker select the
workspace, while the filesystem ID, opaque persistent directory handle, and
inode verify its identity. Device numbers can change after reboot without
changing the directory; a matching recorded stable baseline accepts that change
automatically. The same algorithm is used for ext4, XFS, Btrfs, bcachefs, ZFS,
and other drivers that expose these capabilities. It also works when the private
oc-box registry is on a different filesystem from the project.

Existing registrations are upgraded at a normal verified launch, or by repair.
If an old registration already has a device-number mismatch and no stable
baseline, it needs one explicit repair: an unchanged inode alone cannot prove
the filesystem is the same. Driver/kernel support varies: filesystems that do
not provide a usable persistent handle/filesystem ID retain the conservative
device/inode check and explicit repair when it changes. There is no universal
physical-directory identifier on every Linux filesystem. Relying on only a UUID
marker would accept copied/restored directories indistinguishably, so oc-box
does not claim that as replacement detection. Install the updated launcher before
the next reboot, then normally resume each healthy workspace to record its
baseline. No container reset or session import is needed.

For a genuine or unverifiable identity change, keep the marker and use
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

On V2, ordinary copied Build/global tool permission rules are overridden by the
existing final Build-agent wildcard, including their configured denies, as
before. Final permission asks for the actual container Build agent are
auto-allowed without prompting, but explicit session/mandatory-policy denials
and managed-rule denials applying after the private overlay remain denied.
Unknown-agent requests retain native ask/deny outcomes and are never treated as
Build. All non-Build/Plan/child rules and V1 behavior are unchanged.
The existing Bubblewrap plugin is disabled for
the container launch. HTTP service endpoints using localhost in global config
are mapped to `host.containers.internal`, including the host Lemonade endpoint.
Plain host OpenCode retains its normal configuration and permissions.

The V2 helper changes only a final `ask` to `allow`, never a deny passed to it:
an explicit event agent wins,
otherwise it resolves the current session's agent and then the native configured
default, never the parent's agent. A Build parent does not grant allowances to
Plan/child requests. Session permission arrays are not overwritten; question
and MCP elicitation prompts remain separate and may still need input. The final
private `OPENCODE_CONFIG_CONTENT` overlay reenables only the generated Build
helper against lower-priority project plugin removals; managed policy still wins.
oc-box clients receive no `--auto` or client-wide autoaccept setting. New native
evaluator/import/reload fixtures are offline checks, not accepted live host
permission validation.

If you explicitly want native V2 ask autoapproval for a one-off **host** launch,
`opencode --auto --standalone` is an optional choice, using normal host env/auth
and not the oc-box vault. It approves asks, not explicit denials, including plain
host configured denies. The already-
running TUI equivalent is Ctrl+P → Open settings → Sessions → Permissions →
autoaccept (`cli.json` `session.permissions`); that broader preference can
persist and is not enabled by oc-box. Do not use a global all-agent client
setting as a substitute for oc-box's container Build-only default. New live
interactive TUI permission-prompt/reload validation remains pending. The separate
scripted rootless-Podman global-vault fixture has passed native Build tools,
scoped permission checks and reload; see its accepted scope in
[the V2 guide](docs/opencode-2.md#isolated-global-vault-and-build-approval-host-validation).

V2 without an explicit credential policy is global vault-first: when the host
vault is present, all current entries replace ambient automatic token/API-key
and `pass_env` forwarding, including legacy `OPENCODE_AUTH_CONTENT` overrides.
Ordinary native OpenCode saved/OAuth credential refresh and copying continue in
every mode: vault env entries do not replace structured OAuth refresh-token
records. V1, an explicit env override, or a V2 launch without a
vault retain legacy env forwarding; selected mode restricts credential names.
Delivered credentials remain readable by the agent/tools, and copied config/auth
may already contain secrets. Global use deliberately exposes every stored entry
to each default V2 workspace; use selected mode when a project should receive
less. Forwarded vault values never enter Podman argv, host os.environ, config or
registry. The vault/passphrase are never mounted into the container.

Read [SECURITY.md](SECURITY.md) for the trust boundary and reporting guidance.
Rootless containers share the host kernel, have network access, and do not have
a disk quota. Snapshots/archives are not backups against disk or volume loss.

## Global V2 vault and explicit workspace credential overrides

**V2 defaults to global vault-first without an explicit workspace policy.** This
includes existing V2 workspaces with no policy; saved env/selected policies remain
intentional overrides. V1 retains its legacy env-file default. Global mode uses
all current entries on each normal new owner, including the first launch, rather
than saving a frozen automatic name list. Additions/removals and updated values
apply on the next new owner, not to attached TUI/web/session-shell clients.

For encrypted storage on the host, enable the optional vault support:

```sh
# New isolated Python-tool installation:
pipx install 'oc-box[vault]'
# Existing pipx installation:
pipx inject oc-box 'cryptography>=44'
# Or uv (install/reinstall with the extra):
uv tool install --force 'oc-box[vault]'
# GitHub/source installer:
python3 install.py --with-vault
# Or one-line GitHub installer:
curl -fsSL --proto '=https' --tlsv1.2 https://raw.githubusercontent.com/groopcd/oc-box/master/install.sh | sh -s -- --with-vault
```

The GitHub option installs cryptography in the versioned launcher's private
runtime directory using a temporary virtual environment; it never installs
packages into system Python or user site-packages. GitHub upgrades preserve this
opt-in. There is no password-manager daemon or desktop keyring requirement;
the host needs a controlling terminal and normal private Unix file permissions,
including under WSL. Do not put the vault on an exposed project mount or a
Windows-shared path that cannot enforce those permissions.

Create the vault and store secrets **before your first V2 project launch** through
interactive masked prompts (names only in argv). Global use needs no per-project
registration, exit/configure/relaunch step, or selection command:

```sh
oc-box secrets init
oc-box secrets set ANTHROPIC_API_KEY
oc-box secrets set MCP_API_KEY
oc-box secrets set PERSONAL_GITHUB_TOKEN
oc-box secrets set BRAVE_API_KEY
oc-box secrets list            # Unlocks; prints names, never values
oc-box secrets remove OLD_TOKEN
oc-box secrets passwd          # Change the vault passphrase
```

When no vault exists, default V2 launches keep the env-file fallback and concise
setup guidance. They do not automatically
install crypto, create a vault, modify config, or import JSON secrets. An empty
initialized vault still unlocks and delivers no vault entries, with guidance to
run `oc-box secrets set NAME`. A present corrupt vault, wrong passphrase, or
missing crypto support aborts before container setup/reset deletion, without
silently falling back. OpenCode's native saved/OAuth credential refresh and
copying remain independent in every mode, including global vault mode. A vault
containing only a search API key does not disable an existing Copilot OAuth
account. Vault env entries do not replace structured OAuth refresh-token records,
and no plaintext OpenCode JSON secrets are imported or rewritten. Existing
copied/container auth/config values remain independent; no revocation is implied.

Typing/pasting shows `*` characters and a live character count. At **secret
value** and confirmation prompts, press **Ctrl+R** to switch between masked and
visible input; each prompt starts masked. Vault passphrases always stay masked
and cannot be revealed. Use Backspace/Delete and arrow/Home/End keys to correct
input, **Ctrl+U** to clear, Enter to accept, or **Ctrl+C** to cancel. Bracketed
multiline/control-character or oversized pastes are rejected rather than
silently truncated. The input field scrolls horizontally for long values.

Revealed text is cleared when leaving the prompt, but explicitly showing a key
can expose it in terminal recordings, screenshots, scrollback/reflow, or screen
sharing. Masked feedback reveals length, not content. There is still no plaintext
value command-line argument, saved history, or change to the encrypted format.

The host vault is normally `~/.local/share/oc-box/credentials/vault.json`
(`$XDG_DATA_HOME/oc-box/credentials/vault.json` when set), outside project and
container storage. Credentials and their names are encrypted with AES-256-GCM;
Scrypt derives the key from a passphrase (default minimum 8 characters). The private
file is 0600 and its directory 0700. There is no saved key, unlock cache,
passphrase environment variable, plaintext export, or recovery mechanism for a
forgotten passphrase. Back up the encrypted vault and keep its passphrase safe.

Set separate minimum lengths in `~/.config/oc-box/config.json` (or its XDG
equivalent), retaining any existing settings:

```json
{
  "vault_min_passphrase_length": 12,
  "backup_min_passphrase_length": 16
}
```

Both default to 8 and accept integers from 8 to 1024; passwords can be longer
than the selected minimum, up to 1024 UTF-8 bytes. Override for one operation
with `oc-box secrets init --min-passphrase-length 8`,
`oc-box secrets passwd --min-passphrase-length 8`, or
`oc-box backup ... --min-passphrase-length 8`. These rules apply to new/changed
passwords; existing vaults/backups still unlock with their original passwords.

As an intentional override, select only the credentials an **existing** workspace
needs, after exiting its active launcher. These commands do not start/reset a
container or read values:

```sh
oc-box credentials --project ~/devel/demo
oc-box credentials --project ~/devel/demo --credential-mode selected \
  --clear-credentials --vault-secret PERSONAL_GITHUB_TOKEN --vault-secret BRAVE_API_KEY
oc-box --project ~/devel/demo
```

Selected mode replaces automatic `_TOKEN`/`_API_KEY` and custom `pass_env`
forwarding with those explicit names. It permits mixing sourced environment
credentials and selected vault entries, but not selecting the same name from
both. Normal owning start/reset/import/web prompts once to unlock selected vault
names; the passphrase and vault file never enter the container. A missing selected env
variable, wrong passphrase, or missing vault entry aborts before container setup
or reset deletion. `terminal`, list/status, repair, stop, upgrade, and explicit
history sync never unlock the vault. Delivered credentials are still environment
variables accessible to OpenCode, its tools, and other permitted processes.

Selections persist in the host registry as **names only**, independent of the
project and vault. Repeating options appends names; replace them with
`--clear-credentials`. The accepted modes are `env`, `selected`, and `vault`.
Vault mode uses all current entries, refuses nonempty per-name selections, and
does not save an automatic list. To switch an old selection back to global V2:

```sh
oc-box credentials --project ~/devel/demo --credential-mode vault --clear-credentials
```

For a deliberate persistent legacy env override:

```sh
oc-box credentials --project ~/devel/demo --credential-mode env --clear-credentials
```

Env mode can also add selected vault names without restricting existing env
forwarding. A selected vault value wins over an inherited same-named env value
in that mode. Switching mode does **not** remove old secrets from the private
home, copied OpenCode auth/config, raw Podman `--env` settings, other running
processes, source files, session history, swap, or backups. The vault protects
storage before delivery; it does not hide a delivered key from the agent.
See the [complete credential reference](docs/cli.md#credentials) and
[security notes](SECURITY.md) before migrating sensitive credentials.

V1 with saved `vault` mode returns to legacy env forwarding without unlocking
all-global entries; explicit old env/selected vault selections still work as
before. V2-only `--no-vault` skips vault entries for **one owning start/reset/web
invocation**, without saving policy. Global mode uses legacy env forwarding for
that invocation; explicit env/selected modes retain their env constraints.
Attached clients reject it and never unlock/refresh. Information, maintenance,
sync, backup/restore, credential management and other unrelated commands reject
the flag too. For one-off env-file compatibility only:

```sh
. ~/.config/provider-credentials.env
oc-box --project ~/devel/demo --no-vault
# Or plain host OpenCode, which never consumes the oc-box vault:
opencode --standalone
```

Sourcing a file is not the recommended primary V2 workflow. The next normal
oc-box owner uses its saved/default policy again.

## Backup and restore

Version 1.0.0 adds encrypted full and portable workspace backups. Both modes
passed the disposable Linux host/rootless-Podman fixture, including project/Git
edits, private-home database bytes, session IDs, internal paths, rootless
ownership, and stopped restores. See the [validation details](CONTRIBUTING.md#live-backuprestore-validation).

Install the optional host `age` package (provides `age` and `age-keygen`). Then
exit the source workspace's OpenCode launcher and keep host editors/builds idle:

```sh
# Complete project, private home/session DB, registry evidence, and OS image:
oc-box backup --project ~/devel/demo --output /backup/demo.ocbox
# Smaller backup; rebuild the distro OS on first restored launch:
oc-box backup --project ~/devel/demo --portable --output /backup/demo-portable.ocbox
# Both prompt for a masked backup passphrase, separate from the credential vault.
oc-box backup-info /backup/demo.ocbox
oc-box restore /backup/demo.ocbox --project ~/devel/demo-restored
```

Backups are always encrypted with the standard age format. Project/home/registry
archive and image bytes stream directly into encryption; only ciphertext is
temporarily staged. Native age public recipients can replace the passphrase for
unattended backups using `--recipient age1...`; restore/info use the matching
private `--identity FILE`. The host-wide credential vault is **not included**;
back up its encrypted file separately and retain its own passphrase. Outside
env files are excluded too; files inside the project are ordinary project data.

Restore is non-overwriting and creates fresh workspace/container/volume IDs.
It preserves conversation IDs, full live database/subagent history, source/Git
edits, private home, and resource/credential modes and names-only policies. A
restored default/global V2 workspace uses the restoring host's current vault,
not the original vault or old host privileges. It keeps the old
**in-container** project/home paths and maps the new host project there, rather
than rewriting database paths. Thus the host directory may move while the agent
still sees the original internal path. It leaves the new container stopped;
devices and extra Podman settings are recorded but not activated. Reconfigure
required hardware explicitly. Full backups keep the OS image; portable restores
need system packages reinstalled. Compatible CPU architecture is required.

The initial format refuses extra bind mounts/volumes and nested project mounts,
does not copy ACLs/xattrs/SELinux labels, and is not a running-process checkpoint.
Source symlinks are stored, not followed. Restores reject path traversal, device
nodes/FIFOs, duplicate paths, hardlink/symlink escapes, unbounded archive metadata,
and elevated permission bits. Previously stored credentials/auth/history are
included in private home/project/OS files: treat the encrypted backup and its key
as sensitive. Deleting a backup does not revoke those credentials.

See [backup commands and limitations](docs/cli.md#backup) and
[host fixture validation](CONTRIBUTING.md#live-backuprestore-validation).

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
`oc-box --version` uses `1.6.N+gHASH`. The 1.5 and 1.6 series retain N's original 1.0.0
baseline (full-history commit count minus 29), so commits continue advancing N
automatically rather than restarting at zero. Installed copies retain their
source version until upgraded or reinstalled. Existing 0.1/1.0 installations
upgrade through their usual channel; no workspace reset or history migration is
needed for the version-series change.

## Development status

The project is under active development. Tumbleweed host/container launch,
warm resume, host-session import, MCP connection, and mapped NPU access have
been exercised. The automated mainline test uses temporary databases and local
model/MCP/LSP/formatter fixtures; it does not prove Podman isolation or certify
every distro/runtime/device combination.
The earlier selected-vault probes and separate full scripted global-vault run
on `1.5.38+g0611104` remain accepted at their documented scopes. Reviewed new-layout
host reports now also pass global-vault/Build/full restore on clean
`1.6.42+ge66fb39`, and selected-vault V1/V2 full/portable restore, reset and
downgrade/return on `1.6.43+ge06896f`. The latter changes only delegation
instructions from 1.6.42; keep their execution versions distinct. These are
synthetic scripted host tests, not new interactive UI acceptance, real-provider
inference or real-project backups. See [the test strategy](docs/testing.md).

A separate operator-observed run on clean `1.6.44+ga043049` now accepts the
bounded native Build TUI/resize, browser/persistent-shell coexistence,
detach/reattach, independent web closure and owner lease-drain workflow. It uses
synthetic credentials and `--no-vault`, not interactive vault/Plan/reload testing,
real-provider inference or a real-project backup. Keep this interactive evidence
separate from the scripted lifecycle reports above.

Runtime code lives in `src/oc_box/`, with tests/tools/docs in their conventional
top-level directories. Root installer/build entrypoints remain for compatibility.
Run the source CLI without installing into project/system/user Python:

```sh
uv run --no-project --with-editable . oc-box --version
```

```sh
uv run --no-project --with-editable . --with cryptography python -B -m unittest discover -s tests -v
python3 -B tests/smoke_mainline.py
python3 -B tools/build_docs.py --check
```

Other invoked programs retain their own licenses. oc-box is provided without
warranty under the GPL; see [LICENSE](LICENSE) and [NOTICE](NOTICE).
