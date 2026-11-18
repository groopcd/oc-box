# Install and update oc-box

[Documentation](README.md) / Installation

Stable and development use the same installation methods. The dashboard and
other 1.7-only examples below still require a supporting development release;
they are not commands added to the code on `master`.

> **Primary path: OpenCode 2.x.** The same oc-box installer retains legacy 1.x
> support; follow the [legacy setup guide](legacy/README.md) for its different
> credential, integration and terminal behavior.

Install on the **host as your regular user**, without sudo. Choose one
installation owner; installing a second copy is not a repair for a missing
optional feature.

**On this page:** [Prerequisites](#prerequisites) ·
[Recommended install](#recommended-first-install) ·
[Installation methods](#choose-an-installation-method) ·
[Python tools](#install-from-pypi) · [GitHub/source](#install-from-github-or-source) ·
[Updates](#update-an-existing-installation) · [Completion](#bash-completion) ·
[Manuals](#read-the-manuals)

## Prerequisites

- Linux, Python 3.10+ and rootless Podman with pasta networking.
- Delegated CPU, memory and process cgroup controllers.
- A host OpenCode **2.x** installation for these guides. V2 support requires
  oc-box 1.5+; [the compatibility guide](opencode-2.md) records tested versions
  and feature-specific limits. Existing 1.x installations use the
  [legacy documentation](legacy/README.md).
- Git for GitHub/source installation and self-updates; curl for the one-line
  installer.
- Network access for initial image, package and plugin downloads.

After installation, check tools and get feature-specific host commands:

```sh
oc-box doctor
oc-box doctor --feature private-output
oc-box doctor --feature backup --feature vault
oc-box doctor --feature backup-info --json
```

Doctor identifies the affected feature without disabling unrelated commands.
It does not install packages, unlock secrets, start Podman or change workspaces.
Run it on the host for host advice; inside oc-box it reports container
prerequisites only.

A configured V2 vault requires crypto support and fails closed if it is missing.
Without a vault, env-file compatibility remains available. Native OpenCode
saved/OAuth credential refresh/copy is independent of every vault mode.
Missing age on Tumbleweed suggests `sudo zypper refresh && sudo zypper install age`;
other supported hosts use apt, dnf, pacman or apk. See the complete
[seven-distro dependency table](cli.md#host-dependency-installation). These are
host commands, not packages automatically installed in the workspace image.

V2 needs Linux anonymous-memory file support even with `--no-vault`. Older
uv-managed Python builds without `os.memfd_create` use a compatible libc fallback;
see [private-output troubleshooting](cli.md#private-output-capture-and-uv-python).

## Recommended first install

For newcomers, **install directly from GitHub with vault and TUI enabled**:

```sh
curl -fsSL --proto '=https' --tlsv1.2 https://raw.githubusercontent.com/groopcd/oc-box/master/install.sh | sh -s -- --devel --with-vault --with-tui
oc-box --version
oc-box doctor
```

This explicitly opts in to a **published development release**, because stable
does not yet include the TUI. It does not install moving branch HEAD, initialize
the vault or start a workspace. Dependencies are private to the GitHub
installation. The command downloads and runs the public installer; inspect it
first if preferred. Continue with [credentials and first launch](getting-started.md).

## Choose an installation method

| Method | Install | Ordinary update |
| --- | --- | --- |
| **GitHub — recommended for newcomers** | One-line installer above, including vault and TUI | `oc-box upgrade` |
| pipx: isolated CLI from [PyPI](https://pypi.org/project/oc-box/) | `pipx install oc-box` | `pipx upgrade oc-box` |
| uv: isolated CLI from PyPI | `uv tool install oc-box` | `uv tool upgrade oc-box` |
| pip: activated dedicated virtual environment | `python -m pip install oc-box` | `python -m pip install --upgrade oc-box` |

**Installation method and release channel are separate choices.** New installs
default to stable; development is an explicit opt-in to a published release,
not moving branch HEAD. The recommended command above makes that opt-in explicit;
it does not change the installer's no-selector default. Existing saved channel
choices are retained.
Read [update channels](update-channels.md) for selections, nonmutating previews
and older-launcher bootstrap.
The oc-box release number/channel is also separate from the host OpenCode major
version: selecting stable/development does not switch OpenCode 1.x to 2.x.

## Install from PyPI

### pipx

```sh
pipx install oc-box
pipx ensurepath
oc-box --version
```

For V2 environment-variable credentials from the first project launch, install
`pipx install 'oc-box[vault]'` instead, then initialize the vault before opening
the project. See [getting started](getting-started.md).

### uv

```sh
uv tool install oc-box
uv tool update-shell
oc-box --version
```

Use `uv tool install 'oc-box[vault]'` when vault support is wanted. Neither the
standard pipx nor uv installation automatically installs optional extras.

Open a new terminal if the tool manager just added its bin directory to PATH.
Both methods install published releases from
[pypi.org/project/oc-box](https://pypi.org/project/oc-box/).

### pip in a dedicated environment

```sh
python3 -m venv ~/.venvs/oc-box
. ~/.venvs/oc-box/bin/activate
python -m pip install oc-box
oc-box --version
```

Use the activated environment or `~/.venvs/oc-box/bin/oc-box` later. Podman,
host OpenCode and Linux setup remain external requirements. There is no need
for sudo pip or a system/user-site pip installation.

## Install from GitHub or source

### Published GitHub release

For vault and the dashboard together, use the
[recommended first-install command](#recommended-first-install) above.
The alternative below omits the TUI:

The one-line installer selects stable by default, or reuses the saved GitHub
channel:

```sh
curl -fsSL --proto '=https' --tlsv1.2 https://raw.githubusercontent.com/groopcd/oc-box/master/install.sh | sh
```

This downloads and runs the public installer, trusting the published GitHub
source. To inspect it first, download `install.sh`, review it, then run
`sh install.sh`. Add `--with-vault` for optional crypto support; the piped form is
`sh -s -- --with-vault`.

The bootstrap resolves the published release and verifies its tag/commit against
the selected channel branch. It fetches full Git history anonymously into a
temporary directory for the accurate commit count; it does not use or save
GitHub/provider tokens.

### Reviewed source checkout

This installs the reviewed source, not necessarily a published release:

```sh
git clone https://github.com/groopcd/oc-box.git
cd oc-box
python3 install.py
oc-box --version
```

### GitHub installation layout

The installer copies the launcher to `~/.local/share/oc-box` and creates
`~/.local/bin/oc-box`. Add the bin directory to PATH if needed.
The ordinary CLI needs no third-party Python/npm dependencies or documentation
build tools; vault/TUI support is an explicit optional installation.

A complete versioned copy is staged under `~/.local/share/oc-box/releases/`,
then the launcher is switched atomically. Active sessions keep their original
code until exit; earlier copies are retained, not removed while in use. For the
first migration from an old flat installation, exit active oc-box launchers
before reinstalling. The pinned-release guarantee applies afterward.

## Update an existing installation

### Python-package installations

```sh
pipx upgrade oc-box
uv tool upgrade oc-box
# Inside the installation's virtual environment:
python -m pip install --upgrade oc-box
```

Channel-aware `oc-box upgrade` delegates to the verified pipx, uv tool or
dedicated environment owning the running CLI. Unrecognized/custom installations
receive manual guidance. It never uses the GitHub installer for a package-owned
CLI or creates a competing launcher. Editable source installations must update
their checkout separately. Package auto-update stays disabled.

If more than one method is installed, check `command -v oc-box`.
Public development versions use `1.7.N.dev0`; `oc-box --version` also records
`+gHASH` for provenance. Ordinary package installs select stable unless
prereleases are explicitly requested. PyPI releases are separate from ordinary
GitHub commits.

### GitHub installations

```sh
oc-box upgrade --check       # Preview the saved channel without changing it
oc-box upgrade               # Install a published release from that channel
oc-box upgrade --devel       # Explicitly opt in and remember development
oc-box upgrade --stable      # Return to stable; warns if this downgrades code
oc-box upgrade --auto-update on
oc-box upgrade --auto-update off
```

Automatic updates are **opt-in**. Normal installed starts can check the saved
GitHub channel, install a changed release and continue with the same arguments.
Network/update failures print a message and continue with the installed version.
There is no background daemon or update during an active session. Informational
commands, maintenance terminals, reset/purge and direct source-checkout launches
do not check automatically.

Upgrades replace installed program files/manuals, not source checkouts,
containers, workspace/session state, provider credentials or the host OpenCode
version. A locally newer version is retained unless an explicit channel switch
selects an older release. **A launcher downgrade is not a workspace rollback.**
`--check` saves nothing. Downloads use the fixed public HTTPS upstream without
global Git configuration, helpers, hooks or forwarded environment secrets.
These identity checks are not a signed-release or reproducible-build scheme;
see [Security](../SECURITY.md).

Old pre-channel 1.7 launchers need the channel-aware bootstrap, not unsupported
new flags; exact v1.6.60 needs one plain upgrade first. See
[bootstrap compatibility](update-channels.md#bootstrap-and-older-installations).

## Enable optional development features

After a supporting development package is published:

```sh
uv tool install --prerelease allow 'oc-box[tui,vault]'
# Or: pipx install --pip-args='--pre' 'oc-box[tui,vault]'
oc-box tui --read-only
```

For GitHub installations, switching to `--devel` does **not** enable the TUI
extra for the first time. Rerun the bootstrap as your regular host user:

```sh
curl -fsSL --proto '=https' --tlsv1.2 https://raw.githubusercontent.com/groopcd/oc-box/master/install.sh | sh -s -- --devel --with-tui --with-vault
```

Keep `--with-vault` if used; otherwise omit it. Dependencies stay private to the
installed release, without resetting workspaces or deleting history. Later
upgrades retain supported opt-ins. Do not install a second pipx/uv copy just to
repair a GitHub installation. See
[first-time development TUI setup](https://github.com/groopcd/oc-box/blob/devel/docs/update-channels.md#enable-the-dashboard-after-a-github-channel-switch).

## Bash completion

```bash
source <(oc-box completion bash)
```

Add the line to `~/.bashrc` after PATH setup for future shells. This works with
uv, pipx and GitHub installations. GitHub/source installs also add an autoload
file under `~/.local/share/bash-completion/completions/oc-box`; Python packages
ship the same script in their private environment.

Completion handles command-specific options, distros, doctor features and
file/directory paths locally. It never queries Podman or unlocks credentials.
See the [completion reference](cli.md#bash-completion) for reader paths and setup.

## Read the manuals

Prebuilt man and GNU Info manuals are included; installation needs no
documentation build tools:

```sh
man oc-box
info oc-box
```

If local documentation directories are not searched automatically:

```sh
man "$HOME/.local/share/man/man1/oc-box.1"
info --file "$HOME/.local/share/info/oc-box.info"
```

Packaged manuals are in the package's `docs/` and the tool environment's
`share/man`/`share/info`; reader paths may need to be explicit. The readers are
host packages (`man` and `info` on openSUSE). See
[Contributing](../CONTRIBUTING.md#manuals) to regenerate them from `docs/cli.md`.

## Next steps

- [Create a first workspace](getting-started.md).
- [Choose stable/development and understand upgrade ownership](update-channels.md).
- [Manage credentials](guides/credentials.md).
- [Use the dashboard and concurrent clients](guides/clients.md).
