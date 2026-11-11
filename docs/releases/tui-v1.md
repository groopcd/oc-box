# 1.7 Textual manager — candidate release notes

**Unpublished candidate / release-notes template. Host acceptance is pending.**
The selected publication path is **stable after fresh source-pinned host checks**,
not an early preview. This document neither publishes a version nor records a
passing host run. The final version/hash are deliberately not fixed here:
documentation and integration commits advance the commit-derived version.
These are the detailed manager notes for the combined
[1.7 manager and live-sharing candidate](1.7.md); sharing has its own host gate.

Before using this text for a GitHub Release, freeze the final clean candidate,
complete and review the gates below, then insert its actual public version, full
commit, `+gHASH` identity and absolute commit count in the release notes. Do not
add an evidence commit between acceptance and tagging the tested source.

## Candidate highlights

The optional `oc-box tui` manager adds **Status, Settings, Sessions, Terminals
and Logs**. It is separate from OpenCode's native conversation TUI and does not
replace it or add another chat client.

- **Compact diagnostics:** aligned single-line RAM/CPU/Home FS meters, bounded
  Full diagnostics and System panels, expandable details/mounts, and short
  fixed 0–100% CPU/RAM histories. Numeric workspace CPU retains 100% = one core;
  its bar uses observed quota capacity. Unknown/unlimited is not zero.
- **Controlled monitoring:** one-second default Status/followed-log refresh,
  1/2/5/10/30/60-second choices, auto-refresh toggle and no overlapping/queued
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
- **Source-specific actions:** guarded native archive/unarchive and typed
  `DELETE` confirmation. Parent deletion includes native descendants across
  project filters, with a 50-session review ceiling. Managed host copies with
  native children and cascades with running PTYs (including idle shells) refuse
  deletion. Activity checks are process-local, not a cross-process transaction.
  Host archive copies and workspace sources remain independent; later normal
  sync can replace/recreate host copies. Archived workspace roots still sync but
  are excluded from automatic resume.
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
  available for the whole visible screen. Header
  and modal clipboard controls preserve text-input focus. Paste copied uses
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

**Not included:** CPU/RAM/storage editing, live resource-control RPCs, quotas,
volume resizes or store moves. Saved/effective resource settings are informational.
V1 launcher/history compatibility remains required; live manager session APIs
are V2-only. The ordinary CLI still has no third-party Python baseline dependency.

## Installation and quick start

These commands apply **only to a published release containing the manager**;
they do not assert that the current stable PyPI package already has it:

```sh
uv tool install 'oc-box[tui,vault]'
# Or: pipx install 'oc-box[tui,vault]'
oc-box tui --read-only
```

Use `[tui]` alone if vault support is not wanted. A reviewed GitHub installer
containing the feature accepts `sh install.sh --with-tui` or
`sh install.sh --with-vault --with-tui`; the equivalent piped suffix is
`sh -s -- --with-vault --with-tui`. Extras remain isolated and GitHub updates
retain the opt-ins. Normal operation needs a regular-user Linux host TTY.

For unrelated projects, use the installed launcher. To manage oc-box's **own
checkout**, run an isolated non-editable package from that checkout instead:

```sh
uv run --no-project --with '.[tui]' python -I -m oc_box tui
```

Keep trusted-path validation enabled; an exposed editable launcher must refuse.
`oc-box tui --demo` is a synthetic-only preview, with no host services, registry,
logs or native actions. It is not an acceptance test.

## Evidence required before stable publication

No row below is marked passed by preparing these notes. Record the final source
identity, clean state, native executable and harness identities, dependency
versions, original report locations and actual operator observations privately.

| Gate | Candidate evidence required |
| --- | --- |
| Source/package/native CI | Passing exact-source checks, including native V1/V2 and the TUI contracts/native fixture; resolve any current failures and rerun. |
| Distro checks | Review the seven-image matrix; require fresh passing runs for package-map changes. |
| Build-only distribution | `publish.yml` with `publish=false`, wheel/sdist checks with `--with-vault --with-tui`, and a guard against automatic stable PyPI upload from GitHub prereleases. |
| Focused host history/readiness | Fresh reviewed `tools/test_session_sync_podman.py` report on the final clean candidate: all 42 checks true, including nine archive/resume cases; ordered exits `[0,0,0,1,0,0,0,0]`. |
| Global-vault host lifecycle | Fresh reviewed `tools/test_global_vault_podman.py` report on that same source. |
| V1/V2 host lifecycle | Fresh reviewed `tools/test_v2_podman.py` report, including migrated-history checks, on that same source. |
| Textual host observations | Use the fresh interactive fixture's `manager` mode for five tabs, compact metrics/refresh/settings including staged defaults, static Sessions scrolling, source-specific actions/cascades, embedded PTY/focus/resize/detach, visible leases and independent native clients. This mode takes no background outer lease. |
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

After all required reports/checks are reviewed and publication is approved,
fast-forward **the exact accepted commit** into `master`, tag that same source,
publish the stable GitHub Release/PyPI package and compare public artifact hashes.
Until then, do not advance `master`, tag or publish to PyPI. A successful build or
GitHub prerelease is not a substitute for acceptance; manual `publish=true` still
requires the same gate.

See the [manager guide](../tui.md), [test strategy](../testing.md#experimental-workspace-manager)
and [publication checklist](../../CONTRIBUTING.md#release-gate-and-publication-checklist).
Retain GPL-3.0-or-later [LICENSE](../../LICENSE) and [NOTICE](../../NOTICE) in
distributions.
