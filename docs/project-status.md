# Project status and validation boundaries

[Documentation](README.md) / Project status

oc-box is under active development. This page separates current feature
availability from historical acceptance; it is not a new test-execution report.
The primary user guides target OpenCode 2.x; [1.x operating instructions are
legacy](legacy/README.md). Historical V1/V2 evidence remains intact. Numbers such
as oc-box 1.6/1.7 below identify launcher releases, not the OpenCode major.

## Choose the right source of information

| Question | Read |
| --- | --- |
| Which release/channel should I install? | [Update channels](update-channels.md) |
| How will the stable and development series split? | [Stable 1.8 / development 1.9 plan](plans/stable-1.8-devel-1.9.md) |
| What does the experimental development series add? | [1.7 release notes](releases/1.7.md) |
| What remains unaccepted in the dashboard? | [Manager limitations](releases/tui-v1.md) |
| Which source/native version passed a test? | [Testing evidence](testing.md) |
| How do I reproduce checks safely? | [Contributing](../CONTRIBUTING.md) and [test strategy](testing.md) |

## Current development limitations

The 1.7 development series introduced the optional card dashboard, native TUI
image/PDF imports, shared web tabs and optional browser attachments, live
host-file tools and guarded exit-history recovery. The 1.9 source line continues
that work. The initial 1.8 stable and 1.9 development releases are planned to
share the full feature set, with tested bug and security fixes backported from
1.9 to 1.8 after the split. This is a release plan, not a publication or
acceptance claim. Merged changes are not automatically present in an
already-published release. Stable remains the default channel.

Experimental publication does **not** complete unfinished validation. In the
retained manager observations, full restore returned zero but its later
integrity verifier refused `manifest`. Portable UI/storage completion and final
preservation/journal checks remain unaccepted. Final combined-browser acceptance
also remains separately scoped. See [the complete release boundaries](releases/1.7.md);
do not infer acceptance from the presence of a feature or a zero exit alone.

The retained native 2.3.0 recovery host attempt on `631d317` remains **FAILED,
20/29 checks**. Later informational observations do not retroactively complete
that gate. Keep it separate from earlier accepted 2.2.6 recovery evidence; see
the [formal evidence boundary](testing.md#focused-external-host-evidence-formal-gate-remains-incomplete).

Offline unit/native/process tests do not establish live rootless-Podman
isolation, real-provider behavior, every distro/runtime/device combination or
security certification. Each release must meet its
[final-source gates](releases/1.7.md#final-candidate-and-publication-gates).

## Historical observations retained from the project overview

These are deliberately source-scoped records, not blanket claims for current
development HEAD:

- Tumbleweed host/container launch, warm resume, host-session import, MCP
  connection and mapped NPU access have been exercised. Mainline automation
  uses temporary databases and local model/MCP/LSP/formatter fixtures.
- Earlier selected-vault probes and the full scripted global-vault run on
  `1.5.38+g0611104` retain their documented accepted scope.
- New-layout reports passed global-vault/Build/full restore on clean
  `1.6.42+ge66fb39`, and selected-vault V1/V2 full/portable restore, reset and
  downgrade/return on `1.6.43+ge06896f`. Only delegation instructions changed
  between those sources; execution identities remain distinct. These are
  scripted synthetic host tests, not new interactive UI acceptance,
  real-provider inference or real-project backups.
- Operator-observed clean `1.6.44+ga043049` covers bounded native Build TUI/resize,
  browser/persistent-shell coexistence, detach/reattach, independent web closure
  and owner lease drain. Synthetic credentials and `--no-vault` do not establish
  interactive vault/Plan/reload, real-provider or real-project acceptance.
- Clean `1.6.46+gacddbd8` passed ten provider-free vault-edge reports:
  live/new-owner entry removal, initialized-empty/absent vaults, and
  wrong-password/corrupt/missing-crypto start/reset refusals with state retained.
  This adds no inference, backup, OAuth-refresh or interactive-UI claim.
- Image-free clean `1.6.48+g7df5074` passed six shared-home component checks and
  30 audited hashes: rootless UID/GID 0/1, modes/content, source preservation and
  matching destination fingerprints through the encrypted stream. This is not
  a new per-kind container lifecycle or first-mount pass.

Stable [v1.6.58](https://github.com/groopcd/oc-box/releases/tag/v1.6.58) records
reviewed external history/readiness, global-vault and V1/V2 lifecycle reports,
including the migrated superseded-assistant correction. That reconciliation
does not re-review raw reports or accept the later manager/recovery changes.
The [testing record](testing.md#published-v1658-evidence-reconciliation)
preserves the historical qualifications and later evidence.

## Development identity and source layout

Runtime code lives in `src/oc_box/`; tests/tools/docs remain in their conventional
top-level directories. Root installer/build entrypoints remain for compatibility.
See [source development](../CONTRIBUTING.md#source-layout) for commands.

Current development source uses `1.9.N.dev0+gHASH`; the planned stable `1.8.N`
release has no `.dev0`. These series retain the original 1.0.0 baseline: N is
the full-history commit count minus 29. Installed copies retain their original
source identity until upgraded or reinstalled. A new series number alone does
not require a workspace reset or history migration.
