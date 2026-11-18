# OpenCode 2.x documentation

[Project overview](../README.md) / Documentation

The main guides cover **OpenCode 2.x**, the current development focus. Start with
a walkthrough, choose a task guide when you know what you need, and use the
reference for exact commands and compatibility limits.

## Choose your OpenCode version

| OpenCode line | Documentation path |
| --- | --- |
| **2.x — primary** | [Get started with 2.x](getting-started.md) · [Current task guides](#how-to-guides) |
| **1.x — legacy** | [Use the dedicated legacy guide](legacy/README.md) for setup, credentials, terminal behavior and migration boundaries |

Legacy does **not** mean unsupported or removed. Version-specific runtime
behavior, compatibility checks and regression coverage remain in place. Shared
security/reference material and historical reports still describe both lines.

> **Choose the right release:** stable is the default; 1.7 development features
> are an explicit opt-in. Documentation at branch HEAD may describe changes that
> are not yet published. Read [update channels](update-channels.md) and
> [1.7 limitations](releases/1.7.md) before enabling experimental features.

## Start here

| Your goal | Page |
| --- | --- |
| Understand the project and its trust boundary | [Overview](../README.md) · [Security](../SECURITY.md) |
| Set up the host and install oc-box | [Installation](installation.md) |
| Create and resume a first workspace | [Getting started](getting-started.md) |
| Choose stable/development or fix optional TUI installation | [Update channels](update-channels.md) |

## How-to guides

Each guide focuses on a workflow. Requirements, warnings and links to the full
reference stay with the relevant commands.

| I want to… | Guide |
| --- | --- |
| Resume, stop, reset, repair or remove a workspace | [Workspace management](guides/workspaces.md) |
| Import a host conversation or understand synchronized history | [Workspace management](guides/workspaces.md) |
| Configure the vault or limit credentials per project | [Credentials](guides/credentials.md) |
| Create, inspect or restore encrypted workspace archives | [Backup and restore](guides/backups.md) |
| Use the dashboard, browser and persistent session shell | [Interfaces and concurrent clients](guides/clients.md) |
| Choose between host drops, clipboard imports, snapshots and live file tools | [Host files and attachments](guides/host-files.md) |
| Port optional plain-host plugins to V2 | [Host plugin migration](host-plugins.md) |
| Enable Bash completion or locate installed manuals | [Installation](installation.md) |

## Reference

Use these pages for exact options, defaults, supported versions and behavior.
The [command reference](cli.md) is the source for the installed man/Info manuals;
task guides do not replace it.

| Reference | Contents |
| --- | --- |
| [Commands and options](cli.md) | Every CLI command, flag, configuration path, lifecycle rule and troubleshooting entry |
| [OpenCode 2.x compatibility](opencode-2.md) | Native version differences, environment/service strategy, history and downgrade |
| [Workspace dashboard](tui.md) | Installation, sections, keyboard controls, settings, clipboard and guarded operations |
| [Update channels](update-channels.md) | Published release selection, installation ownership and upgrade policy |

### Architecture and integration details

These pages explain implementation boundaries, not additional user-facing
commands or broader security guarantees.

- [Core concepts](concepts.md): storage, configuration, permissions and the
  host/container boundary.
- [Native TUI selection reporting](ui-state.md): freshness, ownership and
  exact-session routing.
- [Common web shell](web-shell.md): native iframe/tab integration and addon
  boundaries.

### Legacy OpenCode 1.x

Use the [legacy documentation](legacy/README.md), rather than applying 2.x
service, global-vault or persistent-terminal assumptions to a 1.x installation.
The shared CLI reference retains explicitly marked 1.x rules for completeness;
the compatibility guide retains migration/downgrade comparisons.

## Troubleshooting and safety

- Start with `oc-box doctor` and the [CLI troubleshooting reference](cli.md#troubleshooting).
- For identity or duplicate-registration problems, follow the
  [workspace recovery guide](guides/workspaces.md), not a reset or marker deletion.
- Before sharing host files or credentials, read [Security](../SECURITY.md).
- To report a sensitive issue, follow [security reporting](../SECURITY.md#reporting-issues).

## Contributing, testing and releases

| Need | Page |
| --- | --- |
| Developer setup, checks, packaging and contribution workflow | [Contributing](../CONTRIBUTING.md) |
| Current test strategy and historical source-scoped evidence | [Testing](testing.md) |
| Understand what is and is not accepted | [Project status](project-status.md) |
| Development feature scope and publication gates | [1.7 release notes](releases/1.7.md) |
| Dashboard-specific limitations and outstanding validation | [Manager release notes](releases/tui-v1.md) |
| Distribution channel publication policy | [Channel guide](update-channels.md#publication-policy) |
| Maintainer announcement workflow | [Release announcements](release-announcements.md) |

### Historical and planning material

These records preserve the scope of earlier work. They are not substitutes for
current usage instructions or proof that a later source revision passed a gate.

- [Sharing host acceptance](sharing-host-acceptance.md) and
  [implementation handoff](sharing-handoff.md).
- [Release records](releases/) and [design plans](plans/).
- [Testing evidence](testing.md), including exact source identities and retained
  failures.

## Read offline

The installation includes prebuilt command-reference manuals:

```sh
man oc-box
info oc-box
```

See [manual paths and readers](installation.md#read-the-manuals). This GitHub
documentation tree also includes guides and maintainer records that are not
all installed as local manual pages.
