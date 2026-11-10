# Experimental workspace manager

`oc-box tui` is an optional Textual dashboard for inspecting oc-box workspaces,
managing native OpenCode 2.x sessions, and attaching to their persistent session
terminals. It is separate from the native OpenCode conversation TUI opened by
`oc-box start`; it does not replace that client or add a new chat interface.

**Status: experimental development-branch interface, not a stable-release
acceptance claim.** The contract below describes the integrated manager target.
Check `oc-box tui --help` in the installed build for availability; previously
published packages need not include it. Demo/headless checks and native offline
fixtures do not establish interactive host or rootless-Podman acceptance.

## Design and scope

The compact tables, resource histories, and tabbed layout take visual inspiration
from [gputaskman](https://github.com/shanselman/gputaskman). oc-box uses Textual
with its own workspace/session integration; this is visual inspiration, not
copied gputaskman code or a claim of equivalent GPU monitoring.

The first manager is deliberately bounded:

- Inspect host/workspace status without starting a workspace.
- Show saved and effective settings without changing resource limits.
- Connect explicitly to native V2 session catalogs and keep sources distinct
  from independent host archive copies.
- Embed the existing native persistent-session terminal client rather than
  creating a replacement container shell.
- Keep log search available throughout the dashboard, not only while following
  new entries.

**All on-the-fly CPU, RAM, and storage changes are deferred to a separate
project.** There are no live resource edits, storage quotas, volume resizes, or
Podman graphroot/runroot moves here. The existing explicit CLI `reset` workflow
remains outside the dashboard; browsing never resets, syncs, starts, or unlocks
a workspace automatically. There is no `podman update` action or resource-edit
extension to the workspace owner's service.

## Installation

For a Python release containing the manager, opt in to its dependencies through
an isolated tool installation:

```sh
uv tool install 'oc-box[tui]'
# Or:
pipx install 'oc-box[tui]'
```

To include the independent vault extra too, use `'oc-box[tui,vault]'`. Upgrade
through the same tool manager, not `oc-box upgrade`.

For a reviewed GitHub installer containing this feature:

```sh
sh install.sh --with-tui
# Or enable both optional features:
sh install.sh --with-vault --with-tui
```

Review the downloaded `install.sh` before running it. The equivalent piped
bootstrap suffix is `sh -s -- --with-tui`, or
`sh -s -- --with-vault --with-tui`. GitHub installations keep these optional
dependencies in the private immutable release, retain the opt-in across GitHub
updates, and leave running releases pinned to their original copy. No system or
user-site pip installation is needed. Normal non-TUI CLI operation retains its
stdlib-only baseline; the UI dependencies are optional, not prerequisites for
`start`, `status`, or recovery commands.

The initial UI dependency ranges are `textual>=8.2.8,<9` and
`pyte>=0.8.2,<0.9`. These are optional dashboard dependencies, not replacements
for host Podman or OpenCode.

## Launch and navigation

```sh
oc-box tui
oc-box tui --project ~/devel/demo
oc-box tui --workspace WORKSPACE_ID --read-only
oc-box tui --demo
```

Syntax:

```text
oc-box tui [--project PATH | --workspace ID] [--read-only] [--demo]
```

Run normal mode on the Linux host as your regular unprivileged user, with real
TTY input/output. A selector narrows the dashboard to the chosen existing
project/workspace; without one it offers the registered workspace overview.
Opening the manager does not create an environment or become its lifecycle
owner. `--read-only` disables session mutation and terminal control/input; it is
not a promise that an explicitly requested native host connection cannot start
the native host service.

`--demo` supplies synthetic workspaces, metrics, sessions, terminals, and logs.
It permits a container preview, but never contacts host services, reads the real
workspace registry or host logs, or performs host/workspace actions. Synthetic
entries are not evidence of live connectivity or isolation. Normal mode does
not gain a container-mode bypass from the existence of demo mode.

| Key | Action |
| --- | --- |
| `1`–`5` | Status, Settings, Sessions, Terminals, Logs |
| `r` | Refresh the current view |
| `Ctrl+L` | Open Logs and focus the log search |
| `q` | Quit outside a focused embedded terminal |
| `Ctrl-]` | Detach the focused session-terminal client |

When terminal input is focused, ordinary keystrokes belong to the shell rather
than dashboard shortcuts; `Ctrl+L` remains the log-search shortcut. Detach first
to return to ordinary dashboard navigation.
Closing the dashboard disconnects only its clients and releases their leases;
it does not directly stop the workspace owner or end its native persistent shell.
If the owner was already waiting to exit, releasing its final client lease lets
normal sync/stop proceed; the shell does not survive a container stop.

## Five tabs

### 1. Status

Status shows host CPU/RAM, short in-memory resource histories, workspace state,
saved/effective limits, filesystem capacity and mount information. Cheap
workspace metrics are batched and refresh every five seconds by default.
Unavailable readings are shown as unavailable, not as zero.

Allocated project/home/container storage scans are a separate explicit action,
not part of every refresh. They can be expensive and remain estimates:
reflinks, compression, shared image layers and filesystem accounting do not
establish exclusive physical consumption or quota headroom. Reading a mount or
filesystem capacity does not authorize changing it. See the
[resource report reference](cli.md#resource-reports).

### 2. Settings

Settings compares saved workspace limits with observed effective values and
offers runtime dashboard refresh controls. Resource settings are informational;
there are no Apply buttons for CPU/RAM/storage changes, implicit resets, or
storage migrations. Adjusting UI refresh does not change the workspace's
resources or credential snapshot.

If you deliberately need different create-time limits, exit the relevant
clients/owner and use the existing CLI workflow outside the manager:

```sh
oc-box reset --project ~/devel/demo --cpus 8 --memory 16g
```

That command recreates the container OS; it is not a live resize. Review the
[reset reference](cli.md#reset) first. Native config hot reload does not require
restarting the host service and does not import new vault values or host config
into a running workspace.

### 3. Sessions

Session discovery is an explicit **Connect**, followed by manual refresh. Merely
visiting the tab never discovers/starts a service or synchronizes archives.
Choose the connection before interpreting a selected session:

| Connection | Scope and side effects |
| --- | --- |
| Host global | The complete paginated native V2 inventory across projects, including children and archived sessions. Explicit Connect uses normal native CLI discovery and **may start the native host service**. |
| Host project | Native project sessions plus controller-directory archive associations only where their oc-box provenance can be verified. An archive title alone is not verification. |
| Existing V2 workspace | The already-running private workspace service, through an explicit client lease. No owning start, vault unlock, or credential refresh; never redirect it to the host service. |

An incomplete/invalid inventory is not a complete list or evidence that missing
sessions were deleted. A same-looking session ID or title in another store does
not authorize a mutation there. Disconnect releases the workspace client lease;
while connected, that lease can keep an exiting workspace owner waiting before
its final sync/stop. The lease remains held until Disconnect or dashboard exit;
the dashboard makes this owner-wait effect visible. A host connection is
independent of workspace services.

Favorites are private dashboard state, keyed by source/store/session identity.
They are not native OpenCode pins and do not modify native pin APIs or
transcripts.

Native V2 **archive/unarchive** changes the selected native session's archive
state; it is not oc-box history export. **Delete** is a guarded native recursive
deletion: inspect the selected source/store and descendant scope, then type
`DELETE` to confirm. There is no bulk-delete operation. The manager refuses
mutations when activity/ownership cannot be established or work is active or
unconsumed. It does not abort work to make deletion possible.

Native activity checks describe the connected server process, not all processes
that might access host history. A process-local connection carries a race
warning: another host client can change a session after validation and before
mutation. These guards are not a cross-process transaction or a guarantee that
recursive deletion cannot race.

The workspace source and its host archive are independent copies. Deleting or
archiving a host copy does not delete/archive the workspace source; changing a
source does not instantly update its mirror. Later normal oc-box sync may
replace a managed host copy or recreate a deleted one from a still-present
source. This manager adds no permanent mirror suppression or bidirectional
editing. Existing [sync and recovery rules](cli.md#sync) still apply.

OpenCode 1.x remains supported by the existing launcher/CLI. The new live session
manager APIs are V2-only; this guide makes no native V1 archive/unarchive claim.

### 4. Terminals

The embedded view uses an optional `pyte` terminal emulator around a **local PTY
running the existing native persistent-session terminal client**. It is not a
new `podman exec` shell, a maintenance shell, or a mini chat interface. Terminal
creation or taking interactive control requires an explicit confirmation; a
read-only view does not authorize shell input.

Before attachment, the selected source session must be verified as a member of
the connected native store/workspace. A foreign session or independent host
archive copy is not authority to attach to a workspace shell. A live private V2
workspace owner is required for its session terminals; browsing never starts it.

The native PTY owns shell state. `Ctrl-]` detaches the local client but leaves
the native shell available for reattachment. Typing `exit` in that shell ends
it. Dashboard resize is forwarded through the existing client; TUI/web clients
may regain interactive control independently. Closing the dashboard releases
its leases, not the owner or other clients. See the
[native terminal reference](cli.md#terminal) for the existing client boundary.

The emulator is not a complete xterm implementation. Mouse reporting, images,
complex grapheme rendering and extended keyboard protocols are not supported;
use the native standalone terminal client for applications needing those
capabilities.

Actual embedded rendering, keyboard input, resize, takeover and detach/reattach
need a new operator-observed host acceptance run. Headless emulator output and
the earlier native terminal tests are not an embedded-dashboard host pass.

### 5. Logs

The case-insensitive **literal** log search stays visible across all five tabs.
`Ctrl+L` opens Logs and focuses it. Search applies to retained entries and can be
changed at any time, including while follow is paused. Source filtering and
follow/pause controls never disable search; it is not a regular-expression or
shell-command field.

The default oc-box activity source is the private rotating JSON-lines journal
`log/activity.jsonl` under the configured oc-box state root:
`OC_BOX_STATE_HOME`, otherwise `$XDG_STATE_HOME/oc-box`. With neither override,
the file is `~/.local/state/oc-box/log/activity.jsonl`. Rotations are
`activity.jsonl.1` through `activity.jsonl.3`: four files capped at 256 KiB each.
Files use mode 0600 and private directories 0700; readers never create journal
files.

Entries contain fixed event categories and allowlisted metadata, **not raw argv,
environment values, native stdout/stderr, error messages, prompts, or
conversation transcripts**. The UI retains at most 2000 log entries globally
and displays at most 1000 generated-journal entries per refresh. Rotation means
this is not a permanent audit archive. It is not a terminal recording, and this
first stage adds no native OpenCode logfile reader. Demo mode reads none of
these files.

**Podman container logs are a separate, explicit opt-in source.** Only a selected
verified oc-box-owned container may be queried: `--tail` 500 lines, a 1 MiB byte
cap and a 10-second timeout. The dashboard keeps retrieved content in memory
only; it never appends it to the oc-box activity journal. Common credential
patterns are redacted on a best-effort basis, **not proof that arbitrary secrets
have been removed**.
Container output can contain private text or unrecognized credentials. Treat
that display as sensitive; do not share screenshots or terminal recordings
without your own review. Opting in is not permission to read unrelated
containers or unlimited historical output.

## Phases and validation gate

This document defines the experimental branch contract, not a completion or
test-execution report. Integration proceeds in bounded layers:

1. Transport-free session domain, private favorites and fixed-category logging.
2. Optional installation, five-tab UI, cheap metrics and synthetic-only demo.
3. Explicit native host/workspace connections and guarded V2 session actions.
4. Existing-client PTY embedding, interactive behavior and lease cleanup.
5. Clean-candidate host acceptance and review before any stable publication.

Each integrated layer needs behavior-focused tests; declaring a phase does not
mark it passed. See the [manager test plan](testing.md#experimental-workspace-manager).
Keep the tested full commit, clean/dirty state, native executable identities and
manual observations with the evidence. Existing stable host reports validate
their own earlier source and scope, not this new dashboard. No resource-editing
phase is included: CPU/RAM/storage updates, quotas, resizes and store moves remain
separate work.
