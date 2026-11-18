# Create your first workspace

[Documentation](README.md) / Getting started

> **OpenCode 2.x.** For an existing 1.x installation, use the
> [legacy quick start](legacy/README.md#legacy-quick-start).

This walkthrough creates a project environment, explains what is exposed, and
shows how to return to it. Run oc-box on the **Linux host as your regular user**,
not with sudo or from inside another oc-box workspace.

## Before you start

- Install host OpenCode **2.x**, Python 3.10+ and rootless Podman with pasta
  networking.
- Ensure CPU, memory and process cgroup controllers are delegated.
- Have Git and curl available for the recommended GitHub installation below,
  or choose another method from the [installation guide](installation.md).
- Read the [trust boundary](../SECURITY.md): the selected project is writable,
  containers share the host kernel and have network access.

### Install oc-box

**First installation — recommended GitHub path with vault and TUI:**

```sh
curl -fsSL --proto '=https' --tlsv1.2 https://raw.githubusercontent.com/groopcd/oc-box/master/install.sh | sh -s -- --devel --with-vault --with-tui
oc-box --version
oc-box doctor
```

This explicitly selects a **published development release**, because stable
does not yet include the dashboard. Dependencies stay private to the GitHub
installation. This does not initialize or unlock a vault, change credential
policy, launch the dashboard or start a workspace. The command downloads and
runs the public installer; review it first if preferred.

**Already installed?** Use the existing installation, without rerunning curl:

```sh
oc-box upgrade --devel --vault --tui  # Add both extras and select development
# Or request either extra independently:
oc-box upgrade --devel --vault
oc-box upgrade --devel --tui
```

These upgrade flags require a launcher release that supports them and a
published target supporting the requested extras. On an older channel-aware
launcher, upgrade normally first, then request extras. A source merge does not
make that release available. Requests are additive: omitted extras are not
removed, the same release can gain extras, and repeating a satisfied request is
a no-op. The command uses the verified existing installation owner; it does not
create a second copy.

For stable without the dashboard or Python-tool installations, see
[installation alternatives](installation.md). Genuinely pre-channel launchers
have a separate [bootstrap compatibility route](update-channels.md#bootstrap-and-older-installations).

Check the host OpenCode version too:

```sh
opencode --version
```

The doctor command above reports missing dependencies; it does not install them,
start Podman or unlock credentials.

## 1. Choose authentication

Native OpenCode saved/OAuth authentication is copied and refreshed independently
of the vault in every credential mode.

For provider or MCP **environment-variable** credentials, enable the optional
vault support with `oc-box upgrade --vault` on a supporting launcher, or request
it during first installation (`oc-box[vault]` or GitHub `--with-vault`).
Then initialize it **before the first V2 launch**:

```sh
oc-box secrets init
oc-box secrets set ANTHROPIC_API_KEY
oc-box secrets set MCP_API_KEY
```

Only names appear in command arguments. Values and passphrases are entered
through masked host-terminal prompts. Configure provider/MCP `{env:NAME}`
references, not literal values in JSON; see the
[published configuration examples](opencode-2.md#provider-and-mcp-references-not-json-secrets).

Default V2 workspaces receive all current entries from an existing global vault
on each new owning start. If a project should receive less, use an explicit
[credential policy](guides/credentials.md). An initialized empty vault unlocks
but supplies no entries; an absent vault retains env-file compatibility and
setup guidance. The [legacy credential guide](legacy/README.md#credentials)
documents the different 1.x defaults.

## 2. Launch a project

```sh
mkdir -p ~/devel/demo
oc-box --project ~/devel/demo
```

Initial setup downloads the selected image and installs baseline tools:
Node/npm, Python/uv, Bash, Git, curl, ripgrep, PDF tools and the **exact host
OpenCode version**. Named startup stages show progress. The default on
Tumbleweed is `registry.opensuse.org/opensuse/tumbleweed:latest`; use
`oc-box distros` to inspect available images and the host default.

Your project is mounted writable at the same absolute path. The private named
volume uses your home pathname, but is **not your host home**. You can install
system packages inside the environment; later launches reuse its OS layer,
private home, dependency caches and conversation history.

## 3. Exit and return

Exit OpenCode normally. oc-box synchronizes conversation archives to the host
and stops the environment. Return with the same command:

```sh
oc-box --project ~/devel/demo
```

The container and saved conversation are reused. Stopped processes must be
started again. Concurrent clients and activity can delay shutdown; see
[interfaces and owner lifetime](guides/clients.md).

## 4. Inspect without launching

```sh
oc-box list
oc-box --status --project ~/devel/demo
```

Status does not start or stop containers. Expensive storage scans and native
session connections have separate explicit actions.

## Next steps

- [Manage workspaces](guides/workspaces.md): new conversations, imports, reset,
  repair, resources and deletion boundaries.
- [Back up before changing environments](guides/backups.md).
- [Use the optional dashboard and concurrent clients](guides/clients.md).
- [Choose host-file and attachment workflows](guides/host-files.md).
- [Find a command or troubleshoot](cli.md).
