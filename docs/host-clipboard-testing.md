# Host clipboard images and copied files — short manual check

This is an experimental development-source check, not a software release or a
security certification. Use an innocuous screenshot, not credentials or private
desktop contents. No new test harness or broad regression run is needed.

## Preparation

- Use a normal **host** terminal, not a shell inside oc-box.
- Use direct Konsole or Kitty without tmux/screen/SSH.
- Use native OpenCode 2.x. New minor/patch versions, including 2.3.2, are not
  rejected solely for their number; the private-client/transfer/terminal
  contracts must still work. Native composer behavior is a host observation,
  not something advertised by a public capability endpoint.
- Wayland: ensure `wl-paste` is installed (`wl-clipboard`).
- X11: ensure `xclip` is installed.
- KDE/global shortcuts and custom native bindings must not consume Ctrl+Y.

The host import shortcut is **Ctrl+Y**, replacing the
earlier Ctrl+Alt+Shift+V chord. It is unassigned in the
[current OpenCode 2.x default bindings](https://opencode.ai/docs/cli/keybinds)
checked on 2026-11-18. Ctrl+V remains ordinary paste and Ctrl+S/Q retain their
terminal meanings. Ctrl+Y is reserved throughout this opted-in client, including
shell panes, so it no longer performs shell yank there. Without
an effective clipboard enable, Ctrl+Y stays native. Printable `y`/`Y` text is not
intercepted. Clipboard importing defaults to off when no workspace choice is saved.
`--host-clipboard` saves enabled; future native TUI clients inherit it without
repeating the flag. `--no-host-clipboard` saves disabled. Running clients do not
live-toggle, and no clipboard read happens until the dedicated shortcut.

Startup shows a short notice for each enabled import feature. Use
`oc-box start --verbose=ALL` with your normal workspace selector to see
the full behavior, limits and current clipboard symlink policy.

Clipboard file sources now follow symlinks by default, including linked folders.
There is no approval popup or folder setup. Copy the file in Dolphin and use
Ctrl+Y as usual. A link changed after Copy can select a different target at
import time; protected targets and kernel magic links remain refused.
Use `--host-clipboard --no-host-clipboard-symlinks` on a fresh client to restore
strict no-symlink imports. Both choices are saved for future workspace clients;
the strict choice remains saved even if clipboard importing is later disabled.
Use `--host-clipboard-symlinks` to save link following again. Either symlink flag
requires an effective clipboard enable (saved or explicit). Normal file drops
and live sharing remain strict.

`uv --with .` builds the **current checkout**, not a named development branch.
An installed release or another checkout will not acquire the new flag from
that command. Keep your existing checkout/work unchanged and create a separate
test worktree. From your normal oc-box repository on the host:

```sh
git fetch origin devel
git worktree add --detach ../oc-box-host-clipboard-test origin/devel
cd ../oc-box-host-clipboard-test
uv run --no-project --refresh-package oc-box --with '.[vault]' python -I -m oc_box start --help
```

Confirm the help lists `--host-clipboard`, `--no-host-clipboard` and both symlink
flags **before** launching a workspace.
For a release check, replace `origin/devel` in the worktree command with the
exact candidate commit, and use a disposable registered workspace with
synthetic clipboard contents.
If the test directory already exists, do not overwrite or force-remove it;
use another new directory or verify its revision. `--refresh-package oc-box`
also avoids reusing an older local package build when testing branch updates.

If you deliberately deleted this exact test worktree directory but Git still
remembers it, repeat its `git worktree add --detach` command with `--force` to
reuse that missing path's registration. Do not use this to replace an existing
checkout or prune unrelated worktrees; a nonempty directory is not overwritten.

Now use the branch launcher without replacing your published installation:

```sh
# Record the existing policy before saving a test choice:
uv run --no-project --refresh-package oc-box --with '.[vault]' python -I -m oc_box \
  preferences --project ~/devel/demo
uv run --no-project --refresh-package oc-box --with '.[vault]' python -I -m oc_box \
  --project ~/devel/demo --host-clipboard
```

Replace `~/devel/demo` with the **existing registered project** you intend to use.
Do not pass a literal placeholder such as `/path/to/your/project`.

The package is non-editable and isolated. Omit `[vault]` only if your normal
workspace does not require the optional crypto dependency. Select your intended
project explicitly; normal owner/credential prompts and workspace safeguards
still apply. No reset, purge, repair or history reimport is needed.

An existing private owner may stay running: the command opens an additional TUI
client, saves the clipboard enable and does not refresh configuration or
credentials or unlock the vault. Existing clients stay unchanged. For
a new owning launch, `--image-preview` may be added for inline visuals; it is
not accepted as a live toggle while another owner is active.

After testing, restore the intended choices explicitly; do not reset or edit
private preference files. For example, using the same branch launcher:

```sh
uv run --no-project --refresh-package oc-box --with '.[vault]' python -I -m oc_box \
  preferences --project ~/devel/demo --host-clipboard off
```

`preferences --host-clipboard on|off` and
`preferences --host-clipboard-symlinks on|off` work without opening a client,
starting/unlocking the workspace or discovering desktop helpers, including from
a non-TTY. Unlike native startup symlink flags, the symlink preference can be
saved while clipboard importing is off; it matters on a later enabled client.
Only policy booleans are saved, never clipboard data or helper paths.
Stop/reset retains these host-only choices;
restoring into a fresh workspace does not reactivate them.

## Host clipboard packages

The correct Wayland utility is **`wl-paste`**, not `wpl-paste`. Its package is
`wl-clipboard` on every supported distro. Check in your **normal host terminal**:

```sh
command -v wl-paste
```

This only looks up the command; it does not read the clipboard. A check inside
oc-box describes the container, not the host. The feature's startup preflight
performs this host PATH check without reading clipboard data. Explicit
`--host-clipboard` reports the selected utility/package and refuses if missing.
A saved enable instead skips unsupported terminals, native V1 or a missing
desktop helper without erasing the choice. Web/terminal/manager/sync/info commands
ignore it and reject explicit startup flags. Packages are never installed
automatically.

| Host distro | Wayland package installation |
| --- | --- |
| openSUSE Tumbleweed | `sudo zypper install wl-clipboard` |
| openSUSE Leap | `sudo zypper install wl-clipboard` |
| Ubuntu | `sudo apt install wl-clipboard` |
| Debian | `sudo apt install wl-clipboard` |
| Fedora | `sudo dnf install wl-clipboard` |
| Arch | `sudo pacman -Syu --needed wl-clipboard` |
| Alpine | `sudo apk add wl-clipboard` |

Use your distro's normal privilege helper (`sudo`/`doas`, or a root shell).
Refresh package indexes first if needed. These are manual suggestions, not
package-installation tests or new automatic container dependencies.
For an **X11** session, check `command -v xclip` and use package **`xclip`**
in the corresponding command instead. Wayland selection does not silently fall
back to an X11 clipboard if `wl-paste` is missing.

## Five small observations

1. **Clipboard screenshot and files:** copy the image itself in Spectacle, focus the native
   prompt and press **Ctrl+Y**. Look for an attachment/draft, not just
   host `/tmp/Spectacle...` text. The bridge must not send automatically.
   Send manually only if you want the model to receive the image.
   Then copy a small PDF **file** in Dolphin (Ctrl+C), rather than selected text
   inside a PDF viewer, and use the same shortcut. Also try a small PDF plus an
   image with spaces in their names. Supported clipboard file lists should
   produce native attachments without changing/moving the original files.
2. **Existing drops:** directly drag a supported image with spaces in its name,
   or a small PDF, into a file-importing-enabled TUI. Existing behavior should
   remain. In Konsole keep **Disable drag & drop menu for files & URLs** enabled;
   the optional menu's Paste Location action is unframed and intentionally not
   intercepted. If you have saved a file-drop opt-out, do not change it merely
   to test clipboard paste.
3. **Normal controls:** ordinary text paste and Ctrl+V in a focused native shell
   pane remain ordinary controls. They must not import host clipboard images.
4. **No image / opt-out:** with plain text copied, the dedicated shortcut should
   show only a fixed refusal, never the text. A fresh client launched with
   `--no-host-clipboard` must not read the desktop clipboard through this feature;
   future flagless clients must inherit disabled. Conversely, after an explicit
   enable, a fresh flagless supported client should inherit the shortcut. Changing
   policy must not live-toggle the existing test client.
   `--no-host-drop --host-clipboard` should still allow clipboard PNGs and copied
   supported files while leaving drag/path-paste imports disabled (that explicit
   file preference persists). If checking strict symlink capture, save
   `--no-host-clipboard-symlinks` while clipboard is enabled and verify that a
   fresh flagless client still refuses linked sources; restore your intended
   policy afterward.
5. **Exit:** quit the test client normally. Observe any fixed refusal/cleanup
   warning; don't restart or inspect other resources to hide a failure. Cleanup
   is best-effort and does not recall history, provider or copied bytes.

The chord must reach the host relay as the legacy Ctrl+Y byte or a distinct
CSI-u/ModifyOtherKeys report.
If it does nothing, don't remap ordinary Ctrl+V or enable broad filesystem/socket
mounts. Report the terminal and native versions so the binding can be adjusted
in development.

On a revision that includes relay recovery, paste more than 16 KiB of ordinary
synthetic text as a single bracketed paste, without sending it. After its valid
paste end, clear the draft and try Ctrl+Y with a known supported clip. The
ordinary paste should be unchanged, and the shortcut should still work. Do not
deliberately feed malformed terminal traffic to a real workspace. The
`input-framing-recovered` notice at exit means the relay passed a frame through
without using it for import and resumed at an unambiguous boundary; it did not
retry any clipboard read. An `input-framing` or `input-incomplete` warning means
the relay could not trust a boundary and did not resume interception from that
problem. A fresh client can attach if the owner is still running.

If `clipboard-unexpected-read` occurs, recovery can use only a later qualifying
Ctrl+Y event after verified helper reaping. A successful read does not restore
that allowance. There is no automatic retry, input replay or restart. If
staging or helper cleanup cannot be verified, do not try again in that client.
This procedure records an observation only when run on the intended revision.

## What to report

- Branch commit and `oc-box --version`.
- Host OpenCode version, terminal name/version and Wayland versus X11.
- Whether clipboard paste produced a native attachment, a pathname, a refusal
  placeholder, or no visible action.
- Whether anything was sent without Enter (must not happen).
- Whether ordinary Ctrl+V/text paste and existing supported file drops changed.
- Which saved clipboard/symlink choices were exercised, whether a fresh flagless
  client inherited them, and whether existing clients remained unchanged.
- Whether copied PDF/image files worked, how many were selected and their rough
  sizes; don't paste the raw clipboard's URI list or private filenames.
- Any **fixed** refusal/cleanup warning. Do not send clipboard contents, private
  terminal recordings, raw environment dumps or credential-bearing logs.

### If copied files refuse

Report the complete fixed marker, for example
`[oc-box: host clipboard import refused: source-guard]`. The suffix is deliberately
not the underlying exception or pathname:

| Category | Meaning |
| --- | --- |
| `clipboard-types` | No supported or valid offered clipboard type |
| `clipboard-helper`, `clipboard-timeout`, `clipboard-size`, `clipboard-read` | Clipboard retrieval/validation failed |
| `clipboard-unexpected-read` | Unexpected read error; one later qualifying Ctrl+Y event can try again |
| `clipboard-cleanup`, `clipboard-internal`, `clipboard-disabled` | Importing stopped for this client; there is no further helper read or replay |
| `file-uri`, `file-list`, `file-list-limit` | Nonlocal/invalid/unsupported file references or list limits |
| `source-guard` | Missing, inaccessible or protected source, or a link refused in strict mode; deliberately uniform |
| `source` | Other guarded source-capture refusal |
| `content-type`, `file-limit`, `batch-limit`, `source-changed` | Unsupported/mismatched content or source/batch checks |
| `client-state`, `client-limit`, `transfer` | Client lifetime state/quota or private transfer failure |

A portal clipboard target does not itself mean the selected URI uses a portal
path. Do not remove protections or paste the PDF/URI contents to guess the cause.
Re-copy the PDF after copying any diagnostic commands, then retry once in a
fresh client built from the intended source revision and report only the marker.

Imported staging is readable by other processes/tools using the same container
user. It is not per-session isolation. Client crashes, retained references,
native history/backups and agent-created copies can outlive normal cleanup.

Copied files are read through typed local URI lists, not arbitrary clipboard
text. Limits are four supported files, 8 MiB/file and 16 MiB/batch. URI lists are
preferred over optional PNG thumbnails, and failures do not silently fall back.
A clipboard offering only plain text or raw PDF bytes is not a copied-file list.
Cut selections are copied only; their sources and the clipboard remain unchanged.
Native/model support for reading PDF content is separate from file transfer.
