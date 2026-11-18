# Connect web, terminal, and dashboard clients

[Documentation](../README.md) / Guides

> **OpenCode 2.x clients.** Legacy 1.x terminal access is maintenance-only;
> see [the legacy guide](../legacy/README.md#workspaces-and-terminal).

Open another view of a running V2 workspace, or inspect workspaces using the
optional experimental dashboard without starting them.

**Prerequisites:** a configured [installation](../installation.md). Shared V2
web/session-shell clients are available in stable 1.6.94; the Textual dashboard,
mirrored web tabs and browser attachments described here require the explicit
1.7 development channel. The dashboard also needs the independent `tui` extra.
An installation method is not a [release channel](../update-channels.md).

**Contents:** [shared native clients](#open-a-shared-web-or-session-shell-client),
[web tabs and attachments](#choose-a-web-session-and-add-browser-files),
[dashboard setup](#enable-the-experimental-dashboard),
[dashboard tasks](#browse-and-act-in-the-dashboard), and
[validation limits](#know-the-dashboard-validation-limitations).

## Open a shared web or session-shell client

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
retains the restricted shell.
See the [V2 client guide](../opencode-2.md#concurrent-web-terminal-and-full-tui-clients).

Plain host `opencode --standalone` is not an oc-box flag: it uses normal host
environment/auth and does not access the oc-box vault. oc-box continues to own a
private foreground V2 service per active workspace. Original V1 host plugins are
not automatically adapted; see the opt-in [host plugin migration tools](../host-plugins.md)
and [core concepts](../concepts.md). Those source-checkout tools are not installed
oc-box commands; their optional compatibility profile and actual host sandbox
enforcement have separate documented acceptance checks.

## Choose a web session and add browser files

In experimental 1.7, the web shell shows all verified open TUI tabs, including
idle conversations, around one unmodified native web view. On a normally prepared
supported V2 owner, a fresh TUI report opens the current conversation and
highlights its root tab; child conversations keep their exact route.
Explicit selection wins:

```sh
oc-box web --project ~/devel/demo --session ses_example --attachments
```

`--attachments` adds browser-selected PNG/JPEG/GIF/WebP/PDF drops and a file chooser
to that same view. Verify native thumbnails/cards before pressing native Send;
the adapter never submits a prompt automatically. Browser/native history and
provider retention still apply. Older running owners cannot acquire the reporting
helper on attach: choose a displayed tab or use `--session` without restarting
them. Missing, stale or ambiguous reports are not guessed from activity or tab
order. Native browser tabs remain separate from mirrored TUI membership.
See the [web reference](../cli.md#web--browser-interface) for limits and privacy.

## Enable the experimental dashboard

Stable 1.6.94 remains the default and does not include the manager. For an
existing installation with a launcher release supporting upgrade extras,
select a published development release and add the dashboard dependencies:

```sh
oc-box upgrade --devel --tui
# Or add the independent vault extra too:
oc-box upgrade --devel --vault --tui
oc-box tui --read-only
```

This works from stable or development without rerunning curl or creating a
second installation. GitHub dependencies stay in immutable private releases;
verified uv/pipx owners use their own manager. Ambiguous plain-pip extras refuse
with manual guidance. Requests are additive and can update the same selected
release; repeating a satisfied request is a no-op. Add `--check` to validate
without changing installation or preferences. The flags do not initialize or
unlock a vault, change credentials or launch the dashboard.

For a first installation, follow the [installation guide](../installation.md).
An older channel-aware launcher must first upgrade normally to a supporting
published release; genuinely pre-channel builds retain their exceptional
[bootstrap route](../update-channels.md#bootstrap-and-older-installations).
Channel selection alone does not request a new extra. An explicit `--tui` request
against stable refuses; an inherited preference survives a stable interval.
See [extras and missing-dependency guidance](../update-channels.md#enable-the-dashboard-after-a-github-channel-switch).

```sh
oc-box tui
oc-box tui --workspace WORKSPACE_ID --read-only
oc-box tui --demo
```

Normal mode requires a regular-user host TTY. Synthetic-only `--demo` uses fixed
sample data and permits container preview without host service/registry/log
access or actions. Demo and offline checks are **not a new interactive/Podman
host acceptance pass**.

An ordinary installed launcher is appropriate for unrelated projects. To manage
the **oc-box checkout itself**, run a non-editable isolated package from that
checkout instead of `--with-editable` launcher code in the mounted project:

```sh
uv run --no-project --with '.[tui]' python -I -m oc_box tui
```

Keep trusted-path validation enabled; see the
[self-workspace launch explanation](../tui.md#launch-and-navigation).

## Browse and act in the dashboard

The seven sections are **Status, Settings, Sessions, Terminals, Logs, Storage,
and Backup/restore**, with shortcuts `1`–`7`. Navy resource cards, a workspace
table and contextual inspector use a compact section picker instead of the
navigation rail on narrow terminals. The workspace picker stays in the header.

- **Browsing is not launching.** It never starts, resets, syncs, or unlocks a
  workspace. Native V2 session connections/actions are explicit. A host Connect
  may start the native host service; workspace connections lease only an existing
  private owner.
- **Refresh without losing position.** Compact aligned meters and fixed-scale
  CPU/RAM histories distinguish unavailable readings from zero. Status/followed-log
  refresh defaults to one second, accepts whole-number intervals from 1–60
  seconds, and never overlaps queries. Sessions are an on-demand snapshot;
  background refresh does not reset table scrolling or selection. The Overview
  activity card shows only already-retained oc-box categories, not external
  output or agent/token/GPU telemetry. `Ctrl+L` opens Logs; search remains
  available while follow is paused.
- **Settings last only for this session.** `s` or `F2` opens the Settings tab,
  not another window. Apply activates edits, Reset stages defaults, and
  Revert/Escape discards drafts. Compact single-row switches retain ordinary
  mouse/keyboard behavior. Preferences cannot weaken CLI `--read-only`.
- **Copy is explicit.** Visible text is drag-selectable, including logs, graphs
  and control labels; releasing the mouse immediately copies the selection
  without activating the control. Graph bars use copyable text glyphs. There
  are no Copy/Paste buttons: `Ctrl+Shift+C` repeats a selection copy,
  `Ctrl+Shift+Y` copies the visible screen, and `F1` explains shortcuts.
  **Paste copied** (`Ctrl+Shift+Insert`) uses only text copied inside this
  dashboard; use the terminal's ordinary paste for the desktop clipboard.
  Clipboard actions never journal text.
- **Terminals reuse native state.** Experimental embedded terminals reuse the
  native persistent-session shell, not a new container shell.
- **Storage has three distinct scopes:** host native session trees, one workspace,
  and all registered workspaces. There is no whole-host database wipe or
  unreviewed bulk removal. Reviewed workspace removal preserves project files
  and host history.
- **Backup/restore stays encrypted and reviewed.** Scoped cards retain the action
  and secret-input boundaries. Existing full/portable workspace archives and
  encrypted native session-tree history use path-first controls, private masked
  prompts inside the dashboard, named stages, elapsed time and fresh-ID
  restoration. Optional recipient/identity controls are under Advanced; ordinary
  password backups need no key-file copying. Nothing silently overwrites existing
  files or starts restored workspaces.

Delayed session queries show animated, source-specific progress and explicit
cancellation. Failed actions use fixed actionable notifications, not raw native
errors. Lost acknowledgements leave the outcome **unverified**, not falsely
reported as unchanged. CPU/RAM limit edits, quotas, resizes and storage moves
remain deferred; resource settings are informational.

## Know the dashboard validation limitations

The owner approved an **experimental development release with incomplete UI
acceptance disclosed**, not a stable manager release. **UI full restore returned
0, but its later integrity verifier refused `manifest`.** Portable UI
Create/Inspect/restore, three-scope Storage completion, designated ciphertext
deletion, original-source removal with retained-data assertions, final
preservation/journal attestation and latest manager Quit remain unperformed or
unaccepted. Do not infer restored integrity or shutdown/lease cleanup.

The [complete retained observations](../releases/tui-v1.md#retained-observations-and-known-limitations)
preserve the verifier details, evidence identities and pending checks.
See the [manager guide](../tui.md) for keys and source-specific actions, and the
[stable publication gates](../releases/tui-v1.md#evidence-required-before-stable-publication).
Earlier native conversation-TUI or scripted lifecycle passes do not accept this
dashboard or every later development revision.

## Related

- [CLI client reference](../cli.md) · [V2 compatibility](../opencode-2.md)
- [Host files and image/PDF importing](host-files.md) · [Backups](backups.md)
- [Development release status](../releases/1.7.md) · [Security](../../SECURITY.md)
