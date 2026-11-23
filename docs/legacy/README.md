# OpenCode 1.x (legacy)

[Documentation](../README.md) / Legacy OpenCode 1.x

OpenCode **2.x is the primary documentation and development focus**. Use the
[current documentation hub](../README.md) for 2.x workflows and the
[compatibility guide](../opencode-2.md) for version comparisons and migration.
This page covers the retained OpenCode **1.x** operating contract.

**Legacy means retained compatibility in oc-box, not removed support.**
Both runtime lines and version-specific regression coverage remain supported;
this is not a security guarantee or validation of every upstream integration.
OpenCode's `1.x`/`2.x` versions are separate from **oc-box** releases such as `1.7`.

## Legacy quick start

Use an existing host **OpenCode 1.x** executable with a normal oc-box
installation. Requirements remain Linux, Python 3.10+, rootless Podman with
pasta networking, delegated CPU/memory/process controllers, and network access
for initial downloads. See [installation](../installation.md) for shared host
requirements and installation methods; its first-workspace walkthrough is 2.x.

oc-box detects the host OpenCode release and installs that **exact version**
as `opencode-ai@VERSION`. It runs unmodified native 1.x, not a client on a managed
V2 workspace service. Historical checks use OpenCode **1.19.5**; that evidence
does not establish identical behavior across all 1.x releases.

```sh
opencode --version  # Confirm that this host installation is 1.x
mkdir -p ~/devel/demo
oc-box --project ~/devel/demo
```

The first launch creates the private home/container, installs baseline tools,
copies supported host configuration/auth, resolves plugin dependencies, and
starts OpenCode. Cold downloads can take time. Exit OpenCode normally to allow
history synchronization and container shutdown. Launch the same command again
to resume; use `--new` for another conversation in the same environment.

Read [Credentials](#credentials) before the first launch: the default 1.x
environment-forwarding rules differ from 2.x global-vault defaults. The selected
project is writable, including Git metadata and untracked files. Keep independent
backups; a container does not prevent project deletion.

## Credentials

OpenCode 1.x defaults to **legacy `env` forwarding**, not automatic delivery of
all global vault entries. For a normal launch, exported host variables ending
in `_API_KEY` or `_TOKEN`, plus `GH_TOKEN` and `GITHUB_TOKEN`, are forwarded when
set. A trusted host env file can supply these variables; oc-box does not turn
arbitrary unexported shell variables into container credentials.

Additional uppercase names can be listed under `pass_env` in host
`~/.config/oc-box/config.json` (or its XDG equivalent). Store **names, not secret
values**, and preserve unrelated keys. See the
[environment reference](../cli.md#environment-variables) for details.

| Saved workspace policy | Behavior with OpenCode 1.x |
| --- | --- |
| No explicit policy | Legacy `env` forwarding |
| `env` | Legacy forwarding, optionally supplemented by explicitly selected vault names |
| `selected` | Only explicitly selected env/vault credential names, plus fixed runtime preferences |
| `vault` | Legacy env forwarding; does **not** unlock/deliver all global entries |

Explicit older `env`/`selected` vault selections still work and can unlock the
vault. **Do not interpret the last row as “1.x never uses the vault.”**
`--no-vault` is V2-only and is refused on 1.x; it is not the way to configure a
legacy workspace.

Inspect names-only policy for an existing registered workspace with
`oc-box credentials --project ~/devel/demo`.

To deliberately use selected encrypted entries, install optional vault support
through your existing installation method, then use masked **host-terminal**
prompts. Exit the workspace launcher before changing its policy:

```sh
oc-box secrets init
oc-box secrets set MCP_API_KEY
oc-box credentials --project ~/devel/demo --credential-mode selected \
  --clear-credentials --vault-secret MCP_API_KEY
oc-box --project ~/devel/demo
```

Initialize only when creating a vault; do not reinitialize an existing one.
Values/passphrases do not belong in argv, JSON examples, logs, project files,
or plaintext temporary files. Informational policy inspection does not unlock.
Wrong passphrases, missing selected credentials, missing crypto, or a corrupt
configured vault must not silently fall back during a selected-vault launch.

Native saved auth copying is independent of env selection; changing policy does
not revoke embedded credentials in copied config/auth, files, or prior data.
Do not assume V2 saved/OAuth refresh/API contracts exist on 1.x or treat
structured OAuth records as vault env entries. Delivered values remain readable
by container tools; the host vault/passphrase are not mounted. See
[credentials](../cli.md#credentials), [secrets](../cli.md#secrets), and
[Security](../../SECURITY.md).

## Workspaces and terminal

The ordinary lifecycle commands remain available on 1.x:

| Task | Command/reference |
| --- | --- |
| Start/resume or create another conversation | `oc-box --project ~/devel/demo`; [start](../cli.md#start) |
| Inspect registrations and resources | `oc-box list`, `oc-box status --project ~/devel/demo`; [status](../cli.md#status) |
| Stop explicitly | [stop](../cli.md#stop); may interrupt work |
| Archive history after launcher exit | `oc-box sync --project ~/devel/demo`; [sync](../cli.md#sync) |
| Rebuild the container OS | [reset](../cli.md#reset); retains project/private home/history, loses OS-layer changes |
| Make or restore an encrypted workspace archive | [backup](../cli.md#backup), [backup-info](../cli.md#backup-info), [restore](../cli.md#restore) |
| Remove workspace storage | [purge](../cli.md#purge); review deletion flags first |

An active 1.x launcher holds the exclusive workspace lock. Exit it before a
second start, sync, reset, maintenance shell, repair, or purge. Read-only
list/status and explicit stop can run from a second host terminal.

Both `terminal` and `terminal --maintenance` open the **maintenance-only shell**
on 1.x, not V2's persistent session PTY. For example:
`oc-box terminal --project ~/devel/demo`.

Maintenance requires a real host TTY and an existing validated container, and
holds the exclusive lifecycle lock. It does not create a replacement, run
OpenCode, refresh config/auth, forward host credentials/custom `pass_env`,
synchronize conversation history, load shell startup files, or persist shell
history. A stopped container is started and stopped again on exit; a container
already running without an owner is left running. Cleanup restores that prior
run state, including on error/interrupt.

Maintenance commands can still modify project/home/OS files and access existing
secrets in files or container processes; this is not another isolation boundary.
See [terminal](../cli.md#terminal). Never delete `.oc-box-id` or rename databases
to repair a workspace; use the explicit [repair workflow](../cli.md#repair).

Reset is not a backup or a history rollback. Plain purge normally preserves
project files and host archives, but removes private workspace storage;
`--delete-sessions` additionally removes recorded archive copies and `--all`
additionally deletes the project. Make an encrypted backup first and read the
canonical command's identity checks, failure handling, and confirmation rules.

## Plugins and language servers

Native tools, MCP servers, formatters, **V1 API** plugins, and language servers
run inside the container. Their executables/project dependencies must be available
there, not merely on the host. Configuration copying/dependency-cache reuse does
not replace these integrations with host tools or disable them for containment.

Keep configuration compatible with your actual 1.x release. The current 2.x
provider/MCP JSON examples are not legacy configuration templates.
The existing private V1 Build hook remains unchanged; do not assume ordinary
copied global/Build permission denies survive the final Build wildcard.
Plan/subagent rules and plain-host permissions are separate.

Bounded adapters for **copied V1 plugins running on V2** do not change the V1
plugin API or host originals. Likewise, the optional plain-host
[V2 plugin migration utility](../host-plugins.md) is not required legacy setup.
Do not use that V2-only migration on a host profile you still need for 1.x;
its recovery snapshot does not make native V2 plugins compatible with V1.
Read [configuration](../cli.md#configuration-and-environment) and the
[compatibility map](../opencode-2.md#compatibility-map) for those boundaries.

## History, migration and downgrade

Live 1.x history stays in the private container home, not a mounted host DB.
Normal exit/sync uses native export/import for namespaced host archive copies;
resume uses the original container session. This is **history archival, not live
replication or a complete backup**. Host-archive turns are not merged back.

For a one-time host conversation import, quit its source client first and use
the matching project and supported native version:

```sh
oc-box --project ~/devel/demo --import-session ses_example
```

Do not routinely repeat import or overwrite conflicts. See
[sessions and recovery](../cli.md#sessions-and-recovery) for verification,
retained exports, and interrupted-import recovery.

**Before switching native OpenCode majors, make an encrypted workspace backup.**
The first V2 launch seeds `oc-box-v2.db` from a consistent SQLite snapshot of
retained V1 `opencode.db`; upstream performs migration. Separate
`sessions.json`/`sessions-v2.json` resume/archive state and archive namespaces
prevent a V2 resume ID or archive from replacing its V1 counterpart.

Selecting a host 1.x executable and launching normally reinstalls the exact
matching container runtime and V1 overlay and selects retained V1 history.
Returning to 2.x resumes its existing progress rather than reseeding it.
**The histories diverge:** this does not backport new V2 turns into V1, merge
new turns bidirectionally, or automatically roll back all workspace state.
Keep user configuration/plugin APIs V1-compatible for downgrade; use the
documented launcher/reset workflow, not manual DB renames or row edits.

Full/portable backups include both private databases and the archived major;
normal launch afterward matches the current host release. They exclude the
global host vault and outside env files, which need separate protection.
See [history and downgrade strategy](../opencode-2.md#history-and-downgrade-strategy).

## Features that require OpenCode 2.x

Do not apply these V2 workflows to a running 1.x workspace:

- The private managed service, concurrent full-TUI/web clients, native browser
  relay, and persistent **session** shell with `Ctrl-]` detach.
- Experimental `share` live host-file tools and `attach` host-image disclosure.
- Native TUI host-drop imports: explicit `--host-drop` refuses 1.x; automatic
  mode skips unsupported clients rather than preventing ordinary legacy startup.
- Dashboard live native session connection/actions, embedded session PTYs,
  native archive/delete APIs, and encrypted native V2 session-tree backups.

The host dashboard can still show metadata for 1.x registrations; browsing,
reporting, and shared lifecycle operations are distinct from V2 live APIs.
See the [dashboard guide](../tui.md) for action availability/experimental limits
and [CLI reference](../cli.md) for exact native-version gates.

## Validation and support boundaries

Both runtime lines retain supported behavior and tests. The mainline fixtures
exercise native 1.x tools/MCP/LSP/formatting; native V1/V2 integration also checks
migration and preserved history. Historical lifecycle reports use unmodified
OpenCode **1.19.5** alongside **2.2.6**, including scoped synthetic selected-vault,
restore/reset, and downgrade/return checks.

Those are source- and version-bound reports, **not acceptance of today's source**
or every 1.x release. Offline fixtures do not establish live rootless-Podman
isolation, interactive UI, real-provider behavior, or a security certification.
See [testing](../testing.md) for exact evidence identities and
[project status](../project-status.md) for outstanding validation.

Legacy labeling does not weaken the [security boundary](../../SECURITY.md):
the writable project, credentials, integrations, network services, and explicit
devices/mounts remain exposure. Report exact oc-box/OpenCode/Podman versions with
redacted reproductions, never auth files, secrets, or whole private databases.
