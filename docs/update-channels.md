# Update channels

> **Two different version choices:** stable/development select the **oc-box**
> release channel, not the native OpenCode major. The workspace follows the
> host's installed OpenCode version. Current guides target [OpenCode 2.x](README.md);
> existing 1.x installations use the [legacy documentation](legacy/README.md).

[Documentation hub](README.md) · [Getting started](getting-started.md) ·
[Installation](installation.md)

**On this page**

- [Select or preview a channel](#select-or-preview-a-channel)
- [Enable the dashboard after a GitHub channel switch](#enable-the-dashboard-after-a-github-channel-switch)
- [Automatic updates](#automatic-updates)
- [Bootstrap and older installations](#bootstrap-and-older-installations)
- [Publication policy](#publication-policy)

For a first installation, start with the [installation guide](installation.md).
This page covers release-channel selection and its compatibility limits.

---

Stable/devel are **release choices**, not installation methods or permanent
version-series mappings. GitHub/curl and PyPI both remain supported.

| Channel | Publication branch | GitHub publication | PyPI version |
| --- | --- | --- | --- |
| `stable` (default) | `master` | Final Release; current stable line is 1.6, including `v1.6.94` | `1.6.N` |
| `devel` (explicit opt-in) | `devel` | Prerelease, for example `v1.7.N.dev0` | `1.7.N.dev0`, in the same `oc-box` project |

A future accepted stable 1.7 release remains stable. Unpublished branch commits
are not channel updates, and the installer does not advertise moving HEAD as a
release. Both distributions retain the full source hash and absolute commit
count in build metadata, even though PyPI versions cannot contain `+gHASH`.
The development CLI/resource identity is `1.7.N.dev0+gHASH`.

**The optional seven-tab manager and temporary sharing belong to experimental
1.7 development releases, not stable 1.6.94.** Development publication is approved
with explicit [acceptance limitations](releases/1.7.md#known-acceptance-limitations).
It does not declare the unfinished dashboard restore/storage sequence passed
or promote the manager to stable. The original native OpenCode **2.3.0**
persisted-history recovery extension and later guarded **stable 2.3.x**
compatibility amendment have focused native/offline coverage, not new integrated
external-host or dashboard acceptance. The shell/subagent storage profile still
requires exact captured-patch agreement and compatible contracts; it does not
certify untested future native behavior or waive refusals. Other integrations
retain their independently documented version limits. See the
[recovery reference](cli.md#web--browser-interface).

The combined development update also includes the card dashboard, saved
per-workspace native host importing (enabled by default on supported clients),
the shared web tabstrip/current-conversation reports and optional browser File
attachments. Existing clients do not live-toggle; save `--no-host-drop` or use
`oc-box preferences --host-drop off` to disable importing for future clients.
The [final combined-browser validation gap](releases/1.7.md#attachment-and-web-validation-limitations)
is explicit; development publication is not a browser/desktop or stable acceptance
claim.

## Select or preview a channel

Run on the host, as your regular user:

```sh
oc-box upgrade --check          # Preview saved choice, stable if none
oc-box upgrade --devel --check  # Preview development; save nothing
oc-box upgrade --devel          # Install and remember development
oc-box upgrade                 # Follow that saved choice
oc-box upgrade --stable         # Return to stable
```

`--stable` and `--devel` are mutually exclusive and apply only to `upgrade`,
never a workspace start. A successful explicit selection is saved per owner.
The GitHub installation's preference and individual package-owner preferences
are separate; one does not change the other's choice. `--check` makes no
installation or preference changes. A failure is not a successful switch.

GitHub installations verify the release tag/commit against the selected branch
and use the staged immutable installer. Package installations delegate to the
verified pipx, uv tool, or dedicated virtual environment that owns the running
CLI. They never invoke the GitHub installer. If ownership cannot be established
safely, the command refuses and gives manager guidance. Editable source copies
must update their checkout separately.

Normal updates do not silently downgrade. An **explicit channel switch** can
select an older release and warns before doing so. This changes program files,
not workspace/session schemas, private volumes, project files or credentials:
it is not a data rollback. Stop active clients, keep appropriate encrypted
backups, and check compatibility before returning from development to stable.
Only extras supported by the target are installed. Current stable 1.6 has no
dashboard; selecting stable does not carry the development manager into it.
An inherited TUI preference is retained so it can apply on a later return to
development. An explicit `--tui` request against an unsupported target refuses.

## Enable the dashboard after a GitHub channel switch

On a launcher release supporting upgrade extras, add dependencies through your
existing installation owner, whether starting from stable or development:

```sh
oc-box upgrade --devel --tui --check  # Validate/preview; change nothing
oc-box upgrade --devel --tui         # Add dashboard dependencies
oc-box upgrade --devel --vault       # Add vault dependencies independently
oc-box upgrade --devel --vault --tui # Add both
```

`--vault` and `--tui` are independent, additive optional dependency requests,
not workspace credential settings. `upgrade` accepts these canonical names only;
the bootstrap keeps its separate `--with-vault`/`--with-tui` options. Without
either flag, channel selection alone does not enable a new extra. Use
`oc-box upgrade --vault` to add vault support on the saved channel; stable does
not include the dashboard, so select `--devel` for it.

The selected release can gain extras even when it is already installed.
Repeating a satisfied request is a no-op. Omitted extras are not removed;
inherited requests are installed where supported, and the dashboard preference
survives a stable interval for a later return to development. Explicitly
requesting an unavailable extra refuses instead of silently ignoring it.
`--check` validates requested support but changes neither installation nor saved
channel/extra preferences.

GitHub installations stage dependencies in a private immutable release, not
system or user-site Python. Verified uv tool and pipx installations delegate
extra requests to their owning manager. Plain pip cannot prove prior requested
extras; ambiguous installations refuse with manual guidance rather than
guessing. Do not create a second installation to supply missing dependencies.
None of these flags changes credential policy, initializes/unlocks a vault,
launches the dashboard, starts a workspace, resets it or deletes history.

If `oc-box tui` reports “Optional TUI dependencies are missing”, use
`oc-box upgrade --devel --tui` on a supporting launcher; curl is not needed.
If the parser lacks these upgrade flags, upgrade normally to a supporting
published release first. A merge to a branch is not publication. Genuinely
pre-channel launchers retain the exceptional
[bootstrap route](#bootstrap-and-older-installations) below.

## Automatic updates

GitHub automatic updates remain off by default:

```sh
oc-box upgrade --devel
oc-box upgrade --auto-update on
# Later:
oc-box upgrade --auto-update off
```

They follow the saved GitHub channel and run only before normal installed
launches, never during an active session. Use separate commands:
`--auto-update on|off` cannot be combined with a selector, `--check`, `--vault`
or `--tui`.
Network failure leaves the installed version available for the normal launch.

Package automatic updating remains disabled. Package `--auto-update on`
refuses; `off` gives package guidance without changing GitHub settings. Direct
package-manager commands are still supported, but their prerelease/downgrade
behavior belongs to that manager; they are not a substitute for channel
selection and verified ownership checks in `oc-box upgrade`.

## Bootstrap and older installations

The channel-aware bootstrap reuses the saved GitHub channel. A first installation
without a preference defaults to a published stable release:

```sh
curl -fsSL --proto '=https' --tlsv1.2 https://raw.githubusercontent.com/groopcd/oc-box/master/install.sh | sh -s -- --stable --with-vault
```

For development, explicitly select `--devel`; the manager and vault are independent
optional features:

```sh
curl -fsSL --proto '=https' --tlsv1.2 https://raw.githubusercontent.com/groopcd/oc-box/master/install.sh | sh -s -- --devel --with-tui --with-vault
```

Review the downloaded installer before running it if appropriate. Its source URL
is not the target release: fetching the entrypoint from `master` does not mean
installing moving master HEAD. Repeating the bootstrap without a selector does
not reset a saved development choice. Selecting an older launcher warns that
workspace/session state is not rolled back.

New isolated package installations default to stable unless prereleases are
explicitly requested. After a development package has been published:

```sh
pipx install --pip-args='--pre' 'oc-box[tui,vault]'
# Or:
uv tool install --prerelease allow 'oc-box[tui,vault]'
# Or, only inside an activated dedicated virtual environment:
python -m pip install --pre 'oc-box[tui,vault]'
```

Use `[tui]` without `[vault]` if appropriate. For an existing channel-aware
installation, use `oc-box upgrade --devel --vault --tui` once its launcher
supports these extras flags, or request either extra independently. Upgrade
normally first if necessary; do not create another owner.
These commands do not claim a particular development version is already public;
check the [release page](https://github.com/groopcd/oc-box/releases).

Package-manager delegation requires recognized receipts and options. Unsupported
versions or custom options receive manual guidance; uv receipt parsing requires
Python 3.11+. Plain pip does not record requested extras, so an installation
advertising extras cannot be updated automatically without guessing what to
preserve. Automatic targets require a compatible, unyanked universal wheel.

- **Exact `v1.6.60`:** run one plain `oc-box upgrade` first. That published
  launcher's old parser does not accept `--stable`/`--devel`; its existing GitHub
  path provides the bridge to channel support.
- **Pre-feature 1.7 builds, including `5573965`:** rerun the channel-aware
  bootstrap for the desired channel. Their old ahead-of-master check can retain
  the development build, so a plain upgrade is not a reliable bridge. For the
  manager, select `--devel --with-tui` explicitly.
- **Older package copies:** update once with the package manager that owns the
  installation, explicitly allowing prereleases for development, then use the
  new channel-aware command.
- **Channel-aware launchers without `upgrade --vault`/`--tui`:** upgrade normally
  to a published release supporting the flags, then request extras through that
  release. Do not send unknown flags to the old parser or assume an unpublished
  branch commit is available through `upgrade`.

Do not reset, purge, reimport history, or change credentials merely to acquire
channel support. Check `command -v oc-box` if multiple installation methods
are present.

## Publication policy

Stable `master`, its `v1.6.94` release/tag and its PyPI files remain unchanged by
the 1.7 development publication. Publish the channel-integrated successor of the
earlier `5573965` candidate on `devel`; do not merge that unaccepted manager line
into stable to obtain channel support. A new source commit derives a new N from
full history minus the original baseline, 29; do not reuse `1.7.110` or imply
that the successor is the exact previously tested source.

Run relevant source/package/native CI and guarded build-only distribution checks
on the new source, concentrating new coverage on channel/install/version/upgrade
behavior. Earlier accepted host reports remain bound to their exact source and
scope. Document unchanged core workspace behavior separately from changed CLI
upgrade paths; neither matching component hashes nor an approved experimental
release is a new full-source host pass. Do not resume retained fixtures or
replay the completed separate CLI lifecycle gate for publication paperwork.
No protected-data, secret-lifetime or supported-version regression is retired.

Stable promotion still requires complete manager acceptance and the applicable
final-source host gates. Runtime/history/lifecycle/credential changes need their
relevant evidence; a prerelease label does not turn offline tests into host
acceptance. Preserve previous accepted reports and failed evidence.

Development requires **all three**: the `devel` publication branch, a GitHub
prerelease, and a public `.dev0` version. A GitHub prerelease must never upload a
regular `1.7.N` version; manual dispatch is not a guard bypass. Tag, public version,
source metadata and branch membership must agree. Publish only the validated
commit, not an arbitrary later branch HEAD.

The workflow retains its validated clean identity through testing and rechecks
the final wheel and sdist. Attach its exact distributions,
`validated-identity.json` and `SHA256SUMS` to the GitHub release. Compare the public
PyPI hashes with those assets. Earlier local/build-only artifacts are not
substitutes, and existing assets must match by checksum before reuse.

See the [publication checklist](../CONTRIBUTING.md#release-gate-and-publication-checklist),
[test strategy](testing.md), [CLI reference](cli.md#upgrade), and
[security model](../SECURITY.md#installation-and-updates). Retain GPL-3.0-or-later
LICENSE and NOTICE in both distributions.
