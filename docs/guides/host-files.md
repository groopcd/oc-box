# Share host files and import images deliberately

[Documentation](../README.md) / Guides

> **OpenCode 2.x only.** These native client features are not legacy 1.x
> attachment or filesystem-access workflows. See the [legacy feature boundary](../legacy/README.md#features-that-require-opencode-2x).

Choose between temporary live read access, native TUI image/PDF importing, a
per-image confirmation broker, or a direct image message.

**Prerequisites:** the explicit experimental 1.7 development channel and a
running/ready V2 workspace as specified below. Stable 1.6.94 is the default;
these features are not made available merely by choosing pipx, uv or GitHub.
See [installation](../installation.md) and [channels](../update-channels.md).

**Contents:** [live host reads](#share-a-file-or-folder-for-live-reads),
[default TUI imports](#import-through-native-tui-path-pastes),
[clipboard images/files](#import-clipboard-images-and-copied-files-with-ctrly),
[per-image approval](#require-independent-host-approval-for-every-image),
and [direct messages](#queue-a-direct-image-message).

To view a container file, no share or host copy is needed:
see [experimental Ctrl-click container previews](../cli.md#experimental-container-file-preview).
For PNGs, an optional **Open in host app** action exports the already-previewed
snapshot only after a separate host confirmation.

**Choose the consent boundary first.** Live sharing needs an initial typed
`share` disclosure consent. On supported TUI clients, host importing is enabled
by default: a qualifying pathname paste anywhere authorizes copying with no
per-file prompt. The broker instead requires independent typed `attach` approval
for every image. None of these modes recalls data already disclosed to history,
copies or a model provider.

| Workflow | What authorizes access | Data | Native input behavior |
| --- | --- | --- | --- |
| Native TUI host imports | A qualifying host pathname paste; no per-file prompt | Images/PDF, up to 8 MiB/file | A native paste; no added Enter |
| TUI `--host-clipboard` | Saved workspace opt-in and host Ctrl+Y; no per-file prompt | PNG pixels or copied image/PDF files, up to 8 MiB/file | Native paste; you send manually |
| Browser `web --attachments` | Explicit addon and actual browser Files | Images/PDF, up to 20 MiB/file | Native composer; you press Send |
| Direct `attach PATH` | Typed `attach`, or explicit risk acknowledgment | One image up to 8 MiB | Queues a native message; **not a draft** |
| `attach --broker` | Independent host approval for every image | One captured image up to 8 MiB | Expiring, session-bound, one-use tool retrieval |
| `share PATH` | Typed disclosure consent for the path | Live file/folder reads | Read-only tools, not a container mount or per-session attachment grant |

These are different adapters, not interchangeable routes to arbitrary host
files. Browser attachments are source-checked on native **2.2.6/2.3.0**.
Native host drops/clipboard accept valid **2.x** releases subject to their
private-client, terminal and transfer contracts; explicit image attachment keeps
its separate **2.2.6/2.2.7/2.3.0** profiles. Browser batches allow eight files /
40 MiB; native TUI imports allow four paths / 16 MiB per batch, with their own
per-client limits. See the [browser reference](../cli.md#web--browser-interface)
and the workflow sections below before choosing one.

## Share a file or folder for live reads

From a second host terminal:

```sh
oc-box share ~/Pictures/Screenshots --project ~/devel/demo
```

Read the disclosure warning and type `share`. OpenCode gets temporary
`list_directory`, `read_file`, and `read_image` tools with a unique server name
printed by the command. Reads are live and read-only; there is no mount,
container restart, saved configuration, or clipboard access. Keep the command
open; Ctrl-C ends further reads. This requires a running V2 workspace and its
experimental client-MCP API. It does not expose a path to shell commands.

**Shared data may be sent to a model provider or retained in history/copies.**
Read-only and later disconnect do not undo disclosure. Hidden files and future
additions to a shared folder are included; prefer a single file when possible.
See [share](../cli.md#share) for confirmation, protected-path refusals, size
limits and lifecycle details. Its 37-check scripted host acceptance belongs to
the earlier exact `5573965` source and native 2.2.6, not automatically to every
later development build; see [the evidence scope](../releases/1.7.md#reviewed-earlier-source-evidence).

## Import through native TUI path pastes

Outside-host importing is enabled by default per workspace on supported native
TUI clients. Save an override while opening a client, or change only the
preference:

```sh
oc-box --project ~/devel/demo --host-drop
oc-box --project ~/devel/demo --no-host-drop
oc-box preferences --project ~/devel/demo --host-drop off
```

Importing requires native OpenCode 2.x and a direct interactive host
terminal without SSH, tmux or screen. It works on owning start/reset and on an
additional `start` TUI client without restarting the owner. The preference is
saved for future clients, not live-toggled in running ones. Normal launches
gracefully skip unsupported clients; explicit `--host-drop` refuses unsupported
setups. It is not a `web`, session-shell or dashboard option.

Exactly one host Konsole/Kitty compatibility marker must identify the profile;
unknown or inherited ambiguous setups refuse. **This is not emulator
authentication.** Konsole uses complete pathname pastes and leaves child output
unchanged; its default direct-drop setting is needed, not the optional raw
**Paste Location** menu action. Only the Kitty profile probes/enables structured
OSC 72 drops after a positive capability reply. Kitty graphics alone is not DnD.

**A qualifying path paste anywhere in an importing-enabled TUI authorizes copying,
without a per-file prompt.** The bridge cannot prove a true drag gesture or
composer focus. It accepts a whole paste of one to four absolute Linux paths
(supported quoting/escaping for spaces), not mixed prose, newlines, traversal,
controls or `file://` text expansion. Protected/link/special/changed sources
refuse. PNG/JPEG/GIF/WebP/PDF require matching extension/signature, with limits
of 8 MiB/file, 16 MiB/batch and 32 files/64 MiB per client. Selected
models/providers may not support PDF; this bridge supplies no DOCX reader or
document converter. For independent host confirmation of every image, use the
broker below instead.

Captured bytes travel over private non-PTY stdin into private container `/tmp`
staging, not argv/env/shell commands or project files. All native TUI/server/
plugin execution stays in the container. Rewritten container paths become a
native paste: **oc-box never adds Enter or automatically submits a prompt**.
The native UI controls composition/previews; optional owning `--image-preview`
can enable image visuals.

Exit cleanup is best-effort, with no restart scavenger. Crashes, open references,
history, copies or backups may retain bytes, and same-user container agents can
read staged files. This is not a content scanner, blanket security guarantee or
a new live-host acceptance pass. See [`--host-drop`](../cli.md#--host-drop),
[security](../../SECURITY.md#opt-in-native-tui-host-drops) and
[required checks](../testing.md#experimental-transparent-native-tui-host-drops).

## Import clipboard images and copied files with Ctrl+Y

Clipboard access is separate from default pathname drops and **off by default**.
Enable it for this and future workspace native TUI clients on a direct
Konsole/Kitty host terminal:

```sh
oc-box --project ~/devel/demo --host-clipboard
# Later native TUI clients inherit the saved choice:
oc-box --project ~/devel/demo
# Or save policy without opening a client:
oc-box preferences --project ~/devel/demo --host-clipboard off
```

`--no-host-clipboard` also saves disabled while opening a new native TUI client.
These host-only preferences survive stop/start and reset; existing clients do
not live-toggle. Explicit enable has strict preflight. A saved enable gracefully
skips unsupported terminals, native V1 or missing desktop helpers without
clearing the choice. Web, terminal, manager, sync and info commands ignore saved
clipboard policy and reject explicit clipboard startup flags.

Wayland requires host `wl-paste` (`wl-clipboard`); X11 requires `xclip`. Copy
image pixels, or copy local PNG/JPEG/GIF/WebP/PDF files in a file manager, focus
the native prompt and press **Ctrl+Y**. Ordinary Ctrl+V is unchanged. Ctrl+Y
is reserved throughout this opted-in client, including shell panes, so its
normal shell-yank action is unavailable there. There is no automatic Send.

Copied files use typed local URI lists, not arbitrary clipboard text. Up to
four files, 8 MiB each and 16 MiB per batch, share the existing private staging
and client quotas. Only the shortcut reads the clipboard; no polling, model/MCP
trigger, desktop socket mount, source move or clipboard modification is added.

**Clipboard sources follow ordinary symlinks by default**, including linked
folders, with no approval popup. A link changed after Copy may select a new
target when Ctrl+Y is pressed. Protected aliases/targets, kernel magic links,
hard links and special files remain refused; descriptor pinning does not freeze
the file's contents. Add `--no-host-clipboard-symlinks` to save strict no-symlink
clipboard sources for this and future clients. `--host-clipboard-symlinks` saves
link following again; either needs an effective clipboard enable. The strict
choice is retained across disabling/re-enabling clipboard imports. You can also
use `preferences --host-clipboard-symlinks off|on` independently, even while
clipboard is off, for a later enabled client. Normal drops and live sharing
remain strict.

An additional client may use these flags without restarting/unlocking the owner.
The `preferences` command needs no TTY, owner restart, unlock, configuration
refresh or desktop-helper discovery. Only policy booleans are saved, never
clipboard contents or a helper path, and restoring a fresh workspace does not
reactivate these host-only preferences. Imported bytes are readable by workspace
tools and may survive in history, provider systems or retained copies.
See the [full reference](../cli.md#--host-clipboard)
and [short host check](../host-clipboard-testing.md). The owner has reported a
successful Ctrl+Y PDF import through a linked folder using the merged devel test
instructions. This is a scoped manual observation, not a full platform/refusal
matrix or security certification; see the [recorded evidence scope](../testing.md#experimental-host-clipboard-attachment-shortcut).

## Require independent host approval for every image

With host importing disabled or unavailable, an outside host screenshot path is
not directly readable by the container TUI. The confirmation broker does **not**
transparently intercept native drop/paste. For repeated screenshots, start it
once in a second **host terminal**, explicitly choosing the intended native
session:

```sh
oc-box attach --broker --session ses_example --project ~/devel/demo
```

Keep it open. Drop/paste the screenshot **path** in that OpenCode conversation
and explicitly ask OpenCode to use the printed server's `request_image`, then
`read_attachment(id)`. The host terminal must independently approve **every file**
by typing `attach`. The request returns an opaque expiring ID; image retrieval
is single-use and bound to the same native caller session via native `_meta`
context. This is a captured image, not live access: no folder listing, text read,
mount, listener, saved MCP configuration or automatic regrant.

## Queue a direct image message

For a direct image user message instead, run on the host:

```sh
oc-box attach /tmp/Spectacle.example/Screenshot.png \
  --session ses_example --project ~/devel/demo
```

The default message text is `Please examine this host image.` Add `--text` to
change it and `--send` to request native queueing with resume. Without `--send`,
native resume is false, but active execution may still consume the queued
message: this is **not a draft chip or a guarantee of stopped inference**.
Direct mode prints a disclosure warning and needs typed `attach` confirmation
unless explicitly authorized with `--acknowledge-risk`. Broker mode forbids
PATH, `--text`, `--send` and `--acknowledge-risk`.

### Conditions shared by broker and direct attachment

Both require an existing ready workspace, `--session`, an exact native
project-directory match and supported OpenCode V2 2.2.6/2.2.7/2.3.0. Observed
archived or staged-revert targets refuse. Only single PNG/JPEG/GIF/WebP snapshots
up to 8 MiB pass the sharing path/content guards. No starts, credential refresh
or vault unlock occur; no native code or plugin relocation is required.

**Native history/provider processing may retain or normalize the image;
disconnect and grant expiry do not undo disclosure.** Paths pasted into
OpenCode are also disclosed. See [attach](../cli.md#attach) for lifecycle and
safety details and [testing](../testing.md#experimental-host-image-attachments)
for its separate, not-yet-host-accepted validation scope.

## Related

- [Browser-selected attachments](clients.md#choose-a-web-session-and-add-browser-files)
- [Security and reporting](../../SECURITY.md) · [V2 compatibility](../opencode-2.md)
- [1.7 limitations and evidence](../releases/1.7.md) · [Workspace lifecycle](workspaces.md)
