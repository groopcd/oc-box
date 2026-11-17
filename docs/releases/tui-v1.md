# 1.7 Textual manager — experimental development release notes

**Approved for experimental development publication with incomplete UI
acceptance disclosed; not stable manager acceptance.** The owner selected the
`devel` channel and `1.7.N.dev0` package line for the channel-ready successor of
`5573965`. Stable 1.6.94/master remain unchanged and do not include this manager.
This document records no new test run and does not declare the unfinished
dashboard sequence passed.

The final version/hash are deliberately not fixed here: documentation and
integration commits advance N, derived from full-history count minus 29.
Resources keep `.dev0+gHASH`, full commit and count. The release must identify its
actual frozen successor, not reuse the earlier `1.7.110+g5573965` identity. These
are the detailed manager notes for the combined
[1.7 development release](1.7.md), including source-qualified sharing evidence.

## Unreleased interaction follow-ups

The follow-up work branches from published `v1.7.117.dev0` (`2f113ff`) and is
integrated into `devel` for the next development release. It is not available
merely by upgrading to that already-published version:

- Sessions Disconnect beside Connect, plus in-field clear controls.
- Debounced filtering and unchanged-view/incremental rendering, preserving
  viewport, source identity and all monitoring samples.
- Shortcut-only clipboard controls and typed reviews only for destructive actions.
- Custom whole-number refresh interval from 1–60 seconds, default 1.
- Path-first encrypted backup/inspect/restore inside the dashboard, private
  TTY-backed password entry, authenticated metadata review and honest stage/
  elapsed progress. Optional keys remain Advanced; no password is saved.

Headless, synthetic process/age and native checks do not complete the historical
UI/host acceptance described below. Relevant clean-source host backup/UI and
cleanup observations remain a separate gate; no old retained fixture is replayed
or failed evidence relabeled by this follow-up.

## Card redesign feature branch

`feature/tui-card-redesign` follows the approved navy/cyan card concept:
responsive section navigation, a globally available workspace picker,
host-summary cards/compact strip, a scoped workspace table and inspector,
retained fixed-category activity, and card-based Storage/Backup presentation.
Built-in theme choices and all existing explicit/native/private-input controls
remain. This design adds no GPU, model, token, billing or agent-call telemetry.

This separate branch is not a new published development release or an accepted
host UI/backup/security gate. Preserve every historical source identity and
unfinished UI qualification below; headless redesign tests do not relabel them.

## Manager highlights

The optional `oc-box tui` manager adds **Status, Settings, Sessions, Terminals,
Logs, Storage and Backup/restore**. It is separate from OpenCode's native conversation TUI and does not
replace it or add another chat client.

- **Compact diagnostics:** aligned single-line RAM/CPU/Home FS meters, bounded
  Full diagnostics and System panels, expandable details/mounts, and short
  fixed 0–100% CPU/RAM histories. Numeric workspace CPU retains 100% = one core;
  its bar uses observed quota capacity. Unknown/unlimited is not zero.
- **Controlled monitoring:** one-second default Status/followed-log refresh,
  whole-number 1–60-second choices, auto-refresh toggle and no overlapping/queued
  queries. Expensive storage scans remain explicit. Demo uses fixed synthetic
  readings, not live host metrics.
- **Grouped settings:** `s`, `F2` or Open all settings opens Monitoring, Display,
  Sessions, Logs, Resources and Safety/terminals. Apply is dashboard-session-only;
  Cancel/Esc discards edits. Reset to defaults stages preferences for Apply or
  Cancel, without changing history, favorites or retained logs. No setting is
  persisted or journaled, and none can weaken CLI `--read-only` mode.
- **On-demand sessions:** explicit native V2 host-global, host-project or existing
  private workspace connections. Global inventories include children and
  archived sessions. The table stays a static snapshot between explicit updates;
  background monitoring does not reset rows, cursor, selection or scrolling.
  Favorites are private local UI state, not native pins.
  Delayed connections visibly identify the source/stage and show animated progress;
  cancellation remains visible until the bounded request finishes.
- **Source-specific actions:** guarded native archive/unarchive and typed
  `DELETE` confirmation. Parent deletion includes native descendants across
  project filters, with a 50-session review ceiling. Managed host copies with
  native children and cascades with running PTYs (including idle shells) refuse
  deletion. Activity checks are process-local, not a cross-process transaction.
  Host archive copies and workspace sources remain independent; later normal
  sync can replace/recreate host copies. Archived workspace roots still sync but
  are excluded from automatic resume.
- **Actionable failures:** deletion refusals show a reason-specific toast and
  persistent selectable notice without private native errors. Unverified mutation
  acknowledgements and completed actions with failed refreshes remain distinct.
- **Storage and encrypted backup/restore:** host individual session trees,
  selected workspace and all registered workspace inventories are separate scopes.
  Typed one-workspace removal preserves host project/history. Existing full/
  portable `.ocbox` archives and encrypted `.ocsession` history use native masked
  terminal prompts, no-overwrite publication and fresh-ID restores. No whole host
  database wipe, arbitrary cache deletion or unreviewed bulk purge is offered.
- **Experimental embedded native PTY:** an optional bounded local emulator wraps
  the existing persistent-session terminal client, not a replacement container
  shell. Creation/takeover requires confirmation; verified source membership and
  an existing private owner are required. Resize and Ctrl-] detach reuse native
  behavior. It is not a full xterm; mouse/images/complex graphemes/extended
  keyboard protocols remain unsupported.
- **Bounded logs:** always-available literal search, source filtering and
  follow/pause. The private fixed-category journal uses four files capped at
  256 KiB each, not raw argv/env/errors/prompts/transcripts or terminal recording.
  Selected owned Podman output is a separate explicit opt-in, bounded to tail
  500, 1 MiB and 10 seconds, with memory-only retention. Common-pattern redaction
  is best-effort, not proof of arbitrary-secret removal.
- **Selection copy/paste:** drag-select visible text throughout diagnostics,
  table/log rows, graphs, native terminal output and control labels; mouse release
  copies immediately without activating controls. Highlights remain visible over
  colored content, and meter bars use meaningful text glyphs. Copy view remains
  available for the whole visible screen through `Ctrl+Shift+Y`; `Ctrl+Shift+C`
  repeats selection copy and `F1` explains shortcuts. Copy/Paste buttons are
  removed. The `Ctrl+Shift+Insert` Paste copied shortcut uses
  only the in-app clipboard; external clipboard text arrives through the host
  terminal's normal paste command. Ordinary shell Ctrl+C/Ctrl+V and read-only
  boundaries are unchanged. Copies never become journal entries or recordings,
  and container-originated OSC clipboard requests remain blocked. Programmatic
  selection, focus and refresh never trigger auto-copy.

Browsing never starts, resets, syncs or unlocks a workspace. Explicit host Connect
may start the normal native host service; workspace Connect leases only an
existing private owner and never unlocks or refreshes credentials. Disconnect/
quit releases manager clients, which can let an already-exiting owner proceed
with normal sync/stop. The read-only CLI fence remains authoritative.

**Not included:** CPU/RAM limit editing, live resource-control RPCs, quotas,
volume resizes or store moves. Saved/effective resource settings are informational.
V1 launcher/history compatibility remains required; live manager session APIs
are V2-only. The ordinary CLI still has no third-party Python baseline dependency.

## Installation and quick start

These commands explicitly request a published development prerelease; ordinary
stable installations do not include the manager:

```sh
uv tool install --prerelease allow 'oc-box[tui,vault]'
# Or: pipx install --pip-args='--pre' 'oc-box[tui,vault]'
oc-box tui --read-only
```

Use `[tui]` alone if vault support is not wanted. The channel-aware GitHub
bootstrap accepts `sh install.sh --devel --with-vault --with-tui`; its equivalent
piped suffix is `sh -s -- --devel --with-vault --with-tui`. Extras remain isolated;
GitHub updates retain supported opt-ins. A current channel-aware CLI can select
`oc-box upgrade --devel`, delegating package upgrades only to the verified owner.
Older pre-channel 1.7 launchers require the bootstrap instead. See
[update channels](../update-channels.md). Normal operation needs a regular-user
Linux host TTY.

For unrelated projects, use the installed launcher. To manage oc-box's **own
checkout**, run an isolated non-editable package from that checkout instead:

```sh
uv run --no-project --with '.[tui]' python -I -m oc_box tui
```

Keep trusted-path validation enabled; an exposed editable launcher must refuse.
`oc-box tui --demo` is a synthetic-only preview, with no host services, registry,
logs or native actions. It is not an acceptance test.

## Retained observations and known limitations

Exact-source `5573965` has reviewed automation and scripted host history,
recovery, vault, sharing and separate V1/V2 lifecycle acceptance; see the
[evidence index](../testing.md#reviewed-5573965-candidate-evidence). The six-stage
CLI lifecycle gate is **complete**, not invalidated by the separate incomplete
UI sequence, and must not be replayed for publication paperwork.

Real navigation, settings, clipboard, loading/focus/resize/native PTY, session
backup/fresh-ID restore and masked-password observations retain their original
scope and `NOT_AUTOMATICALLY_PASSED` qualification. Full workspace backup Create
and Inspect returned 0; authenticated backup verification passed, and the expected
Host whole-workspace-backup preflight refusal was genuinely observed.

**UI full restore returned 0, but its later integrity verifier refused
`manifest`.** A metadata-only observation found the intentional home extraction
root change from 0755 to 0700 with unchanged host UID/GID. This is not all-entry
equality or proof that this mode change was the sole/first refusal cause.
Full restored integrity/preservation/fresh-identity oracle completion remains
unaccepted.

Portable UI Create/Inspect/restore was not armed. Three-scope Storage completion,
designated ciphertext deletion, original-source removal with retained
project/history/retired-provenance/other-ciphertext/both-restores assertions, final
journal/preservation attestation and latest manager Quit confirmation remain
unperformed or unaccepted. Do not infer shutdown/lease cleanup. The owner
approved experimental publication with these limitations, not a pass or further
fixture continuation. Preserve original failures, stores and encrypted evidence.

The channel successor changes install/update/version/publication handling, not
dashboard/core workspace behavior. Its new source still needs relevant
source/package/native and guarded build-only validation; prior evidence is not a
new-source full-map pass. Stable 1.6.94 and newer native-version evidence do not
accept the manager or experimental sharing.

## Evidence required before stable publication

No row below is marked passed by preparing these notes. Record the final source
identity, clean state, native executable and harness identities, dependency
versions, original report locations and actual operator observations privately.

| Gate | Candidate evidence required |
| --- | --- |
| Source/package/native CI | Passing exact-source checks, including native V1/V2 and the TUI contracts/native fixture; resolve any current failures and rerun. |
| Distro checks | Review the seven-image matrix; require fresh passing runs for package-map changes. |
| Build-only distribution | `publish.yml` with `publish=false`, wheel/sdist checks with `--with-vault --with-tui`, and source/tag/version/channel guards that cannot publish a final PyPI version from a GitHub prerelease or manual bypass. |
| Focused host history/readiness | Fresh reviewed `tools/test_session_sync_podman.py` report on the final clean candidate: all 42 checks true, including nine archive/resume cases; ordered exits `[0,0,0,1,0,0,0,0]`. |
| Global-vault host lifecycle | Fresh reviewed `tools/test_global_vault_podman.py` report on that same source. |
| V1/V2 host lifecycle | Fresh reviewed `tools/test_v2_podman.py` report, including migrated-history checks, on that same source. |
| Textual host observations | Use the fresh interactive fixture's `manager` mode for seven tabs, compact metrics/refresh/settings including staged defaults, static Sessions scrolling, visible connection progress/cancellation and failure notifications, source-specific actions/cascades, embedded PTY/focus/resize/detach, visible leases and independent native clients. This mode takes no background outer lease. |
| Storage and encrypted backup UI | Guarded single-workspace removal, retained project/history, active-owner/exposure/read-only refusal, real masked foreground TTY handoff, full/portable restore to fresh directories, encrypted native session-history restore with fresh IDs, and deletion of only the reviewed owned backup file. |
| Journal/log/self-checkout boundaries | Private bounded fixed-category journal and rotation, no synthetic secret/raw-text leakage, owned opt-in bounded memory-only Podman output, and a non-editable self-workspace launch without weakening validation. |
| Temporary host-file sharing | Fresh checks on the same candidate for explicit disclosure consent, live read/image results, confinement, ordinary-container-path isolation, lease/termination/reload behavior, no automatic regrant, backup/source preservation and enforcing SELinux without source relabeling; see [sharing validation](../testing.md#experimental-temporary-host-file-tools). |

Run live checks only from a normal unprivileged **external host**, with fresh
private synthetic state and verified isolated rootless Podman storage. Never run
the host fixtures inside oc-box, use real projects/credentials, move retained
stores or overwrite encrypted evidence. Preserve failed attempts separately.
Manual observations must not be self-certified from scripts or headless tests.

The published [v1.6.58 record](https://github.com/groopcd/oc-box/releases/tag/v1.6.58)
reports reviewed scripted host evidence for its own earlier source. Raw reports
were not re-reviewed for this documentation update; that release and older native
conversation-TUI observations do not accept this Textual candidate. No
real-provider, real-project restore, network-containment or security-certification
claim is added.

The approved development path uses the exact validated successor on `devel`,
one matching `v1.7.N.dev0` GitHub prerelease and PyPI upload, with the workflow's
exact distributions, validated identity and checksums. Compare public artifact
hashes; do not substitute the old build-only artifacts or start a second upload.
It does not advance `master` or create a final stable package.

Stable promotion remains separately gated on complete manager acceptance,
applicable final-source host checks, owner approval and reviewed integration with
the then-current stable branch. These requirements do not schedule another host
run or authorize replay of retained fixtures. A prerelease/manual dispatch is
not a way to upload an unaccepted regular final `1.7.N` version.

See the [manager guide](../tui.md), [test strategy](../testing.md#experimental-workspace-manager)
and [publication checklist](../../CONTRIBUTING.md#release-gate-and-publication-checklist).
Retain GPL-3.0-or-later [LICENSE](../../LICENSE) and [NOTICE](../../NOTICE) in
distributions.
