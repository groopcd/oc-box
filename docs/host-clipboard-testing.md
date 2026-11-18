# Host clipboard image paste — short manual check

This is an experimental feature-branch check, not a software release or a
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
- KDE/global shortcuts must not consume Ctrl+Alt+Shift+V.

`uv --with .` builds the **current checkout**, not a named feature branch.
An installed release or another checkout will not acquire the new flag from
that command. Keep your existing checkout/work unchanged and create a separate
test worktree. From your normal oc-box repository on the host:

```sh
git fetch origin feature/devel-host-clipboard
git worktree add --detach ../oc-box-host-clipboard-test origin/feature/devel-host-clipboard
cd ../oc-box-host-clipboard-test
uv run --no-project --refresh-package oc-box --with '.[vault]' python -I -m oc_box --help
```

Confirm the help lists `--host-clipboard` **before** launching a workspace.
If the test directory already exists, do not overwrite or force-remove it;
use another new directory or verify its revision. `--refresh-package oc-box`
also avoids reusing an older local package build when testing branch updates.

Now use the branch launcher without replacing your published installation:

```sh
uv run --no-project --refresh-package oc-box --with '.[vault]' python -I -m oc_box \
  --project ~/devel/demo --host-clipboard
```

Replace `~/devel/demo` with the **real existing project** you intend to use.
Do not pass a literal placeholder such as `/path/to/your/project`.

The package is non-editable and isolated. Omit `[vault]` only if your normal
workspace does not require the optional crypto dependency. Select your intended
project explicitly; normal owner/credential prompts and workspace safeguards
still apply. No reset, purge, repair or history reimport is needed.

An existing private owner may stay running: the command opens an additional TUI
client with its own clipboard opt-in and does not refresh its credentials. For
a new owning launch, `--image-preview` may be added for inline visuals; it is
not accepted as a live toggle while another owner is active.

## Host clipboard packages

The correct Wayland utility is **`wl-paste`**, not `wpl-paste`. Its package is
`wl-clipboard` on every supported distro. Check in your **normal host terminal**:

```sh
command -v wl-paste
```

This only looks up the command; it does not read the clipboard. A check inside
oc-box describes the container, not the host. The feature's startup preflight
performs this host PATH check and reports the selected utility/package if missing.
It never installs packages automatically.

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

1. **Clipboard screenshot:** copy the image itself in Spectacle, focus the native
   prompt and press **Ctrl+Alt+Shift+V**. Look for an attachment/draft, not just
   host `/tmp/Spectacle...` text. The bridge must not send automatically.
   Send manually only if you want the model to receive the image.
2. **Existing drops:** directly drag a supported image with spaces in its name,
   or a small PDF, into a file-importing-enabled TUI. Existing behavior should
   remain. In Konsole keep **Disable drag & drop menu for files & URLs** enabled;
   the optional menu's Paste Location action is unframed and intentionally not
   intercepted. If you have saved a file-drop opt-out, do not change it merely
   to test clipboard paste.
3. **Normal controls:** ordinary text paste and Ctrl+V in a focused native shell
   pane remain ordinary controls. They must not import host clipboard images.
4. **No image / opt-out:** with plain text copied, the dedicated shortcut should
   show only a fixed refusal, never the text. A client launched without
   `--host-clipboard` must not read the desktop clipboard through this feature.
   `--no-host-drop --host-clipboard` should still allow clipboard PNGs while
   leaving file-drop importing disabled (that explicit file preference persists).
5. **Exit:** quit the test client normally. Observe any fixed refusal/cleanup
   warning; don't restart or inspect other resources to hide a failure. Cleanup
   is best-effort and does not recall history, provider or copied bytes.

The chord must reach the host relay as a distinct CSI-u/ModifyOtherKeys report.
If it does nothing, don't remap ordinary Ctrl+V or enable broad filesystem/socket
mounts. Report the terminal and native versions so the binding can be adjusted
on the feature branch.

## What to report

- Branch commit and `oc-box --version`.
- Host OpenCode version, terminal name/version and Wayland versus X11.
- Whether clipboard paste produced a native attachment, a pathname, a refusal
  placeholder, or no visible action.
- Whether anything was sent without Enter (must not happen).
- Whether ordinary Ctrl+V/text paste and existing supported file drops changed.
- Any **fixed** refusal/cleanup warning. Do not send clipboard contents, private
  terminal recordings, raw environment dumps or credential-bearing logs.

Imported staging is readable by other processes/tools using the same container
user. It is not per-session isolation. Client crashes, retained references,
native history/backups and agent-created copies can outlive normal cleanup.
