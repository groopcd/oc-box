# Update channels

Stable/devel are **release choices**, not installation methods or version
series. GitHub/curl and PyPI both remain supported.

| Channel | Publication branch | GitHub publication | PyPI version |
| --- | --- | --- | --- |
| `stable` (default) | `master` | Final Release, for example `v1.6.N` | `1.6.N` |
| `devel` (explicit opt-in) | `devel` | Prerelease, for example `v1.7.N.dev0` | `1.7.N.dev0`, in the same `oc-box` project |

These are current series examples, not permanent channel mappings. A future
stable 1.7 release remains stable. Unpublished branch commits are not channel
updates. Both distributions retain the full source hash and absolute commit
count in build metadata, even though PyPI versions cannot contain `+gHASH`.

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
installation or preference changes. A failure does not count as a successful
switch.

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
Only extras supported by the target are retained. Current stable 1.6 has no
`--with-tui`; the development TUI extra is separate.

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
`--auto-update on|off` cannot be combined with a selector or `--check`.
Network failure leaves the installed version available for the normal launch.

Package automatic updating remains disabled. Package `--auto-update on`
refuses; `off` gives package guidance without changing GitHub settings. Direct
package-manager commands are still supported, but their prerelease/downgrade
behavior belongs to that manager; they are not a substitute for the channel
selection and verified ownership checks in `oc-box upgrade`.

## Bootstrap and older installations

The channel-aware bootstrap reuses the saved GitHub channel. A first installation
without a preference defaults to a published stable release:

```sh
curl -fsSL --proto '=https' --tlsv1.2 https://raw.githubusercontent.com/groopcd/oc-box/master/install.sh | sh -s -- --stable --with-vault
```

Use `--devel` instead of `--stable` for a published development prerelease.
`--with-vault` is optional and compatible with either selector. The bootstrap
script's source URL is not the target release: fetching its entrypoint from
`master` does not mean installing moving master HEAD.
Repeating the bootstrap without a selector does not reset a saved development
choice. Selecting an older launcher warns that workspace/session state is not
rolled back.

Package-manager delegation requires recognized receipts and options. Unsupported
versions or custom options receive manual guidance; uv receipt parsing requires
Python 3.11+. Plain pip does not record requested extras, so an installation
advertising extras cannot be updated automatically without guessing what to
preserve. Automatic targets require a compatible, unyanked universal wheel.

- **Exact `v1.6.60`:** run one plain `oc-box upgrade` first. That published
  launcher's old parser does not accept `--stable`/`--devel`; its existing
  GitHub path provides the bridge to channel support.
- **Pre-feature 1.7 builds:** rerun the channel-aware bootstrap for the desired
  channel. Their old ahead-of-master check can retain the development build,
  so a plain upgrade is not a reliable bridge.
- **Older package copies:** update once with the package manager that owns the
  installation, then use the new channel-aware command.

Do not reset, purge, reimport history, or change credentials merely to acquire
channel support. Check `command -v oc-box` if multiple installation methods
are present.

## Publication policy

The initial channel-capable stable release belongs to the **1.6 series on
master**. Subsequent **1.7 development releases belong on devel**, without
merging the unaccepted 1.7 line into master to obtain channel support. Existing
published tags and artifacts remain unchanged.

Freeze a clean exact candidate and keep its full history. Run the existing
source/package/native gates, build-only distribution checks and applicable
external-host acceptance before publication. Keep the seven-image distro gate
for package mapping changes. Runtime/history/lifecycle/credential changes still
need their relevant new host evidence on the clean integrated candidate; a
prerelease label does not waive validation or turn offline tests into host
acceptance. Preserve previous accepted reports and failed evidence verbatim.

Publish only the tested commit on its intended branch. Stable uses a final
GitHub Release and public final version; development uses a GitHub prerelease
and `.dev0` version. Tag, public version, source metadata, branch membership,
GitHub assets and the same project's PyPI files must agree. Do not select or
advertise moving HEAD as a release. A later branch/evidence commit does not
silently become an updater target.

See [the publication checklist](../CONTRIBUTING.md#release-gate-and-publication-checklist),
[test strategy](testing.md), [CLI reference](cli.md#upgrade), and
[security model](../SECURITY.md#installation-and-updates). This policy records
no new live host acceptance or publication.
