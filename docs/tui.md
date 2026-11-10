# Experimental workspace manager

`oc-box tui` is an optional Textual dashboard for inspecting oc-box workspaces,
managing native OpenCode 2.x sessions, and attaching to their persistent session
terminals. It is separate from the native OpenCode conversation TUI opened by
`oc-box start`; it does not replace that client or add a new chat interface.

**Status: first-release candidate; host acceptance pending, not published as
stable.** The embedded terminal remains experimental. The contract below
describes the integrated manager, not a test-execution report.
Check `oc-box tui --help` in the installed build for availability; previously
published packages need not include it. Demo/headless checks and native offline
fixtures do not establish interactive host or rootless-Podman acceptance.
The publication decision is **stable after fresh source-pinned host checks**,
not an early preview. See the [candidate notes](releases/tui-v1.md) for the
release-summary template and evidence still required.

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

These installation examples apply to a release/source revision containing the
manager; they do not assert availability in the current stable PyPI package.
For ordinary unrelated projects, use the installed `oc-box tui` normally. The
self-checkout launch below is specifically for managing oc-box's own source.

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
TTY input/output. A selector initially focuses the chosen existing
project/workspace; the sidebar still permits switching registrations.
Without one it offers the registered workspace overview.
Opening the manager does not create an environment or become its lifecycle
owner. `--read-only` disables session mutation and terminal control/input; it is
not a promise that an explicitly requested native host connection cannot start
the native host service.

For developing **oc-box inside its own workspace**, launch an isolated,
non-editable copy from the checkout:

```sh
uv run --no-project --with '.[tui]' python -I -m oc_box tui --project ~/src/oc-box
```

An editable launcher (`--with-editable`) executes trusted host code from that
same mounted project, so attachment correctly refuses it. This is the
`trusted-path-exposure` guard, not a missing owner or terminal failure. A normal
package keeps the launcher outside the mounted checkout; `python -I` also avoids
source-path overrides. Do not disable validation, reset the workspace or replace
its marker to work around an editable launch. Editable source remains suitable
for synthetic `--demo` and isolated tests.

`--demo` supplies fixed synthetic workspace metrics and logs; native session and
terminal connections are disabled. CPU/RAM/storage readings intentionally stay
the same after refresh. They are sample values, not measurements of the host.
Run normal host mode, optionally with `--read-only`, to see live measurements.
It permits a container preview, but never contacts host services, reads the real
workspace registry or host logs, or performs host/workspace actions. Synthetic
entries are not evidence of live connectivity or isolation. Normal mode does
not gain a container-mode bypass from the existence of demo mode.

| Key | Action |
| --- | --- |
| `1`–`5` | Status, Settings, Sessions, Terminals, Logs |
| `r` | Refresh the current view |
| `s` or `F2` | Open all dashboard settings outside focused text inputs or the native shell |
| `Ctrl+L` | Open Logs and focus the log search |
| `q` | Quit outside a focused embedded terminal |
| `Ctrl-]` | Detach the focused session-terminal client |
| `Ctrl+Shift+C` | Copy selected text, when the terminal forwards this shortcut |
| `Ctrl+Shift+Insert` | Paste text copied inside the dashboard into the focused input or connected shell |

When terminal input is focused, ordinary keystrokes belong to the shell rather
than dashboard shortcuts, including `s`, `F2` and the shell's `Ctrl+L`. The
always-visible header log search can still be clicked without ending the shell.
Detach with `Ctrl-]` first to use settings shortcuts, `Ctrl+L` and ordinary
dashboard keyboard navigation again. Settings shortcuts also leave focused text
inputs alone, and background refresh does not take focus from the native terminal.
The compact global header stays on one line and ellipsizes long
labels rather than crowding out controls on small terminals; log search remains
available from every tab.
Closing the dashboard disconnects only its clients and releases their leases;
it does not directly stop the workspace owner or end its native persistent shell.
If the owner was already waiting to exit, releasing its final client lease lets
normal sync/stop proceed; the shell does not survive a container stop.

### Copying and pasting

Drag to select visible text in diagnostics, session/workspace tables, logs and
the embedded terminal. Use **Copy** in the header, or `Ctrl+Shift+C` when the
terminal forwards that key. Selection highlighting alone never writes to the
clipboard. Text fields also retain their normal Textual selection/copy/paste
keys. In the native shell, ordinary `Ctrl+C` still interrupts and `Ctrl+V` keeps
its shell meaning; neither is replaced by a global clipboard shortcut.

**Copy view** copies the current visible screen as plain text, including labels
on buttons and other controls that are not drag-selectable. Settings and review
dialogs have their own clipboard controls. Copying is available in read-only
mode and never confirms a dialog, changes a session, or starts a connection.
Table/log/terminal selections cover the displayed viewport, not offscreen
history. Scroll to the desired text before selecting. Followed-log display stays
in place while its text is highlighted; bounded collection continues in memory.

For text from your desktop clipboard, focus a text field or the connected shell
and use your **host terminal's paste command**—commonly `Ctrl+Shift+V`,
`Shift+Insert`, or its context menu. This delivers a normal terminal paste event.
The dashboard does not read the desktop clipboard in the background or install
clipboard helper programs.

**Paste copied** (also `Ctrl+Shift+Insert`) instead pastes the last text copied
**inside this dashboard**, not arbitrary current desktop clipboard contents.
It targets only the focused, enabled text field or connected native terminal;
it never connects or starts a shell. Single-line fields use normal terminal-paste
semantics; the embedded terminal keeps its existing control filtering and
bracketed-paste framing. Its 64 KiB input bound includes UTF-8 bytes and framing,
and size/full-queue rejection sends no prefix of the rejected paste. A transport
failure can occur after partial delivery; its warning tells you to inspect the
shell rather than blindly retry a possibly partially executed command.

Host clipboard delivery uses Textual's OSC 52 support and depends on the terminal
and its clipboard permissions; it is not guaranteed in every terminal, remote
session or multiplexer. Explicit copies are bounded to 1 MiB and remain only in
the app's clipboard memory and the user-requested host clipboard. The dashboard
never journals clipboard content or adds terminal recordings. Pasting into the
native shell is still normal shell input and may enter its existing native
session/shell history. Treat copied external logs/shell
output as sensitive. OSC clipboard requests emitted by container applications
remain stripped; only an explicit dashboard copy action writes to the host
clipboard.

## Five tabs

### 1. Status

Status presents two compact, bounded panels: gold **Full diagnostics** for the
selected workspace and green **System (OS-reported)** for system readings. Aligned
RAM, CPU and Home FS rows use single-line meters. Short in-memory CPU/RAM
histories use a fixed 0–100% scale, not an auto-scaled sparkline that exaggerates
small changes. Full workspace details and mount information remain available in
an expandable section, initially collapsed; the Settings display controls can
change its visibility.

Workspace CPU's numeric percentage keeps the convention **100% = one CPU core**
and can exceed 100%. Its meter uses the observed CPU quota as capacity when that
quota is known; normalized meter/history values do not replace the numeric
reading. Unavailable or unlimited capacity is identified as such, never invented
as zero. Other unavailable readings are also shown as unavailable. System values
are what the OS reports, not a promise of exclusive resources or physical host
capacity. Presentation does not change underlying totals or the raw report data.

Cheap metrics are batched. Status and followed logs share an adjustable refresh
interval of **1, 2, 5, 10, 30 or 60 seconds**, default **1 second**, with an
auto-refresh control. Cadence is best-effort: a slow query does not overlap or
queue another refresh, so the selected interval is not an exact timing guarantee.
Native session inventories still require explicit Connect and manual refresh.
With no current workspace, System readings and monitoring settings remain
available; missing workspace data does not become a zero-valued workspace.

Allocated project/home/container storage scans are a separate explicit action
for the selected workspace, not part of every refresh. They can be expensive and
remain estimates:
reflinks, compression, shared image layers and filesystem accounting do not
establish exclusive physical consumption or quota headroom. Reading a mount or
filesystem capacity does not authorize changing it. See the
[resource report reference](cli.md#resource-reports).

### 2. Settings

The Settings tab retains quick monitoring controls and an **Open all settings**
button. It compares saved workspace limits with observed effective values;
resource settings are informational, not editable limits.

Use the button, `s` or `F2` to open a categorized modal settings window. Keyboard
shortcuts apply only outside focused text inputs and the native shell. The
window is also usable without a current workspace:

| Topic | Controls and boundaries |
| --- | --- |
| Monitoring | Auto-refresh and the shared Status/followed-log interval: 1, 2, 5, 10, 30 or 60 seconds; default 1. |
| Display | Built-in Textual theme and expanded/collapsed diagnostics details. |
| Sessions | Scope and text filter only; changing them never connects or refreshes a native inventory automatically. |
| Logs | Follow, source and literal search; searches stay in memory and are never persisted or journaled. External sources retain their explicit opt-in and ownership guards. |
| Resources | Read-only saved limits and observed values. |
| Safety/terminals | Read-only information about the CLI-selected mode and terminal safety; the `--read-only` fence cannot be weakened here. |

**Reset to defaults** stages factory dashboard preferences without closing the
window or immediately changing the active dashboard. Defaults are automatic
one-second refresh, `textual-dark`, collapsed details, global session scope,
following all loaded log sources, and empty session/log filters. **Apply** makes
them active; **Esc** or **Cancel** discards the reset. No reset touches containers,
resource limits, history, favorites or retained log entries, and it cannot weaken
the CLI-selected read-only mode.

**Esc** or **Cancel** discards pending window edits. **Apply** accepts only the
allowlisted dashboard preferences, for **this dashboard session only**. Changes
are neither persisted nor logged; they do not modify OpenCode `cli.json`, host
settings, workspace resources or credential snapshots. There are no CPU/RAM/
storage edit controls, resource-control RPCs, implicit resets or storage
migrations. A settings Apply is not a resource Apply or native service reload.

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

The table is a static snapshot between explicit operations. Background status,
log-follow and storage updates do not rebuild its rows or reset the cursor or
scroll position. Use **Connect / refresh**, or `r` while the connected Sessions
tab is active, to fetch new entries. Editing a filter, selecting a project or
performing a session action updates the relevant view on demand.

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

Native V2 **archive/unarchive** changes only the selected native session's archive
state; it is not oc-box history export or a recursive archive operation. Archived
workspace roots remain in history synchronization but are not automatic resume
targets. Archive and resume selection therefore need checking separately.

**Delete** is a guarded native recursive deletion. Deleting a parent includes
its native descendants, even when they are outside the current project filter;
the preview lists the affected native IDs and directories. Inspect the selected
source/store and full cascade, then type `DELETE` to confirm. The dashboard
refuses cascades larger than **50 sessions**, including the selected parent,
rather than presenting an incomplete review. A managed host archive with native
children is refused, not treated as an ordinary recursive-delete target.
There is no bulk-delete operation. The manager refuses mutations when
activity/ownership cannot be established or work is active or unconsumed. It
does not abort work to make deletion possible.

Native activity checks describe the connected server process, not all processes
that might access host history. A process-local connection carries a race
warning: another host client can change a session after validation and before
mutation. These guards are not a cross-process transaction or a guarantee that
recursive deletion cannot race.

Archiving never implicitly closes a running persistent shell. Deletion refuses
running terminals in its cascade, including idle shells, until they exit.
The initial inventory can be cancelled by Disconnect or exit; cancellation is
checked between pages, and its pending lease releases after the current bounded
native request. A late mutation result cannot restore a disconnected/replaced
source's rows.

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

The emulator is not a complete xterm implementation. Native application mouse reporting, images,
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
shell-command field. Follow uses the same best-effort refresh interval as Status
(default one second); pausing follow leaves retained entries searchable. Search
text and settings changes are never saved or written to the activity journal.

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

## Stable-release validation gate

This document describes the candidate contract, not a completed host acceptance
or publication. Its bounded implementation/validation layers are:

1. Transport-free session domain, private favorites and fixed-category logging.
2. Optional installation, five-tab UI, cheap metrics and synthetic-only demo.
3. Explicit native host/workspace connections and guarded V2 session actions.
4. Existing-client PTY embedding, interactive behavior and lease cleanup.
5. Clean-candidate host acceptance and review before any stable publication.

Each integrated layer needs behavior-focused tests; declaring a phase does not
mark it passed. See the [manager test plan](testing.md#experimental-workspace-manager)
and [publication checklist](../CONTRIBUTING.md#release-gate-and-publication-checklist).
Keep the tested full commit, clean/dirty state, native executable identities and
manual observations with the evidence. Existing stable host reports validate
their own earlier source and scope, not this new dashboard. No resource-editing
phase is included: CPU/RAM/storage updates, quotas, resizes and store moves remain
separate work.

Do not advance `master` (the curl/self-update source), create the stable tag or
publish to PyPI before the new candidate passes the required external-host
checks and report review. A build artifact, demo, prior native conversation-TUI
pass or GitHub prerelease is not an alternative to that gate.
