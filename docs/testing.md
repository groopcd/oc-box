# Test strategy and suite audit

[Documentation hub](README.md) · [Contributor checks](../CONTRIBUTING.md#checks) ·
[Getting started](getting-started.md)

**Version scope:** the main user guides target [OpenCode 2.x](README.md);
[OpenCode 1.x is documented as legacy](legacy/README.md). This organization
does not retire supported V1 cases, alter validation gates or relabel historical
reports. Source/native identities below retain their original wording and scope.

**Start with the test guidance**

- [Layers](#layers): choose unit, process, native, packaging or host checks.
- [Ownership map](#ownership-map) and [Reviewing future changes](#reviewing-future-changes):
  find the responsible tests and preserve distinct regressions.
- [Contributor checks](../CONTRIBUTING.md#checks): standard commands and prerequisites.
  Host-only acceptance fixtures must not run inside oc-box or against real
  workspaces or credentials.

**Find feature-specific checks and their limits**

| Area | Sections |
| --- | --- |
| Browser clients and selection reports | [Browser attachments](#experimental-browser-attachment-adapter) · [Web sessions](#open-tui-sessions-in-the-web-interface) · [Native TUI selection reports](#cooperative-native-tui-selection-reports) |
| Host files and dashboard | [Temporary host-file tools](#experimental-temporary-host-file-tools) · [Host-image attachments](#experimental-host-image-attachments) · [Native TUI host drops](#experimental-transparent-native-tui-host-drops) · [Workspace manager](#experimental-workspace-manager) |
| Launcher help, output and activity journal | [Command help](#command-help) · [Quiet lifecycle and severity-based logs](#quiet-lifecycle-and-severity-based-logs) |
| History and readiness | [V2 history synchronization](#v2-session-history-synchronization) · [Focused host runner](#focused-historyreadiness-host-runner) |
| Backups | [Host configuration migration](#host-configuration-migration) |
| Host-only fixture guidance | [Vault edges](#fresh-provider-free-vault-edges-host-only) · [Manual UI observation](#fresh-manual-ui-observation-host-only) · [Home-component ownership](#shared-home-component-ownership-fresh-host-only-image-free) |
| CI and announcements | [Ubuntu CI bootstrap](#ubuntu-ci-package-bootstrap) · [Optional social announcements](#optional-social-release-announcements) |

**Read historical evidence in its original scope**

The feature sections mix test recipes, limitations and retained reports.
Evidence is tied to the source/native identities and scope recorded with each
report, not to the current checkout. Navigation does not promote an unfinished
check, replace a host gate or authorize replaying retained fixtures.

- [Reviewed 5573965 candidate evidence](#reviewed-5573965-candidate-evidence)
- [Migrated-history candidate](#next-migrated-history-release-candidate),
  [published v1.6.58 reconciliation](#published-v1658-evidence-reconciliation) and
  [reviewed host reports — 2026-11-09](#reviewed-host-reports--2026-11-09)
- [Suite audit result](#audit-result): the recorded consolidation and coverage
  comparison, not a current suite pass count.

---

Tests protect behavior, not a target number of methods. A validation table can
have many necessary negative cases; moving them into one method is consolidation,
not permission to stop checking those cases. V1 compatibility is supported code,
not a deprecated feature that can be removed from the regression suite.

## Layers

- **Unit/contract tests:** public CLI/config behavior, failures before destructive
  actions, ownership/identity checks, immutable installations and secret lifetime.
  Import the normal `oc_box` package; avoid private duplicate copies of runtime
  modules, paragraph-by-paragraph documentation assertions or tests of unittest's
  own setup/tempfile behavior.
- **Real local process tests:** age streaming/envelopes, controlling-TTY input,
  terminal restoration, websocket framing and filesystem archive defenses. These
  do not become redundant merely because a parent-process coverage report does
  not instrument their children.
- **Native offline integration:** unmodified OpenCode V1/V2, tools/MCP/formatter,
  history import/export/migration, permission evaluation, plugin compatibility,
  credential delivery and terminal/browser coexistence. Keep actual CLI transport
  checks as well as HTTP helpers: HTTP-only tests missed the 204-response bug.
- **Packaging/distro CI:** build and install wheel/sdist artifacts, check their
  real metadata/resources, and exercise all seven dependency catalogs. Preserve
  the released updater against the new src layout, not just the current updater.
- **Opt-in host acceptance:** synthetic, isolated rootless-Podman lifecycle and
  encrypted backup/restore. Never run these fixtures inside this container or use
  real workspaces/credentials. Native/offline results do not establish a new live
  host pass; retained evidence and each accepted scope remain separate.

### Current OpenCode npm upgrade and advisories

The reusable source CI runs `tests/smoke_runtime_upgrade_v2.py` in addition
to the pinned integration and 2.3 history suites. It checks cold `2.2.6`
owning preparation and warm reuse, then installs OpenCode 2.3.0, upgrades the
same private npm global prefix, and runs owning
preparation against 2.4.0 and the latest stable 2.x resolved by npm (once
if they match). It checks that a synthetic session survives, the required
Build plugin becomes active, and a second launch does not reinstall the CLI
or SDK. It substitutes a local process for `podman exec`; distro package
selection and live Podman behavior still need their own checks.

The exact incident regression also runs `2.3.2 → 2.4.0` in that native
fixture. The runtime safeguard unit fixtures separately exercise isolated
refusal, prior runtime retention, patch rechecks, and durable forward
admission. They are not external-host Podman acceptance or proof that an
older helper can safely reopen the normal host store.

`tools/test_entrypoint_migration_podman.py` is an opt-in fixture for a fresh VM
with local rootless Podman and a disposable user/store. It uses the production
replacement engine twice: once with a real OS snapshot, and once with a single
injected nonzero snapshot result followed by a separately approved OS rebuild.
The fixture checks the unchanged project and private volume identity, SQLite
bytes and mapped ownership, the different OS-layer outcomes, and that the
legacy PATH-shadow command never runs during replacement or subsequent boot.
It checks that the original stays stopped until the replacement passes
verified passive boot and mount access, then retires only that original ID.
It does not run native OpenCode, baseline installation, or a full launcher
lifecycle. Never substitute a real project or backup for the synthetic data.

For an already-fenced `rlimit type 6` failure, the optional
`tools/probe_legacy_start.py --container NAME` diagnostic inspects the affected
container on its rootless Podman host and tests passive startup in a disposable
container from the same local image. It uses no explicit process limits, real
workspace mounts, network, vault, or OpenCode. It shows any startup error from
the probe locally and removes only its verified resource. This is not a
workspace repair or a full launcher acceptance test. It disables host device
defaults and CDI for the probe, and refuses an existing containers.conf
override rather than replacing that selection.

The npm upgrade fixture audits the generated public plugin SDK dependency tree at all
reported severities before installing the resolved tree. An advisory or
failed audit fails the job and therefore blocks PyPI publication through the
reusable source gate. The managed SDK graphs for 2.2.6–2.2.7 and
2.3.0–2.4.1 (the tested releases) scope fixes for
[glob](https://github.com/advisories/GHSA-3g3m-2fjr-6pw4) and
[minimatch](https://github.com/advisories/GHSA-g3hv-75wg-m249) to the SDK;
old SDK caches prepared without those fixes are not reused. Deprecation
warnings are not the advisory check. Updating the managed overrides invalidates
the old copied lock, so other ranges in a custom profile can also re-resolve.
For these releases, the launcher resolves the lock without package scripts
and checks for the two affected ranges locally before `npm ci`; it refuses a
resolution that reintroduces an affected version, even via a custom override.
Managed resolution and `npm ci` use the same shallow layout: npm 10 can
otherwise hoist scoped overrides into a lockfile that its own `ci` rejects.
The unmanaged install path keeps npm's ordinary strategy.
The CI audit does not inspect dependencies
bundled inside upstream executables, distro packages, or private/user plugin
and MCP dependency graphs; no private dependency names are sent to npm audit.
The launcher still matches the exact host OpenCode version rather than
silently switching the workspace to whatever npm currently labels latest.

The latest-stable-2.x web job also runs the production session archive sync
fixture against the resolved native executable. This covers scoped native
exports/imports, retained and pending history, and actual quiet detection
after a held shell. It uses synthetic databases and substituted transport,
not rootless Podman.

The 2.4 recovery CI job pins public Linux x64 executables for 2.4.0 and 2.4.1 and runs
the retained-subagent, graceful foreground/queued-input and abrupt foreground
Reader regressions. This does not replace the older native matrix or the
source-pinned external-host gate. Read-only guard tests separately cover
machine state, unsafe/stale blobs and durable video keys (including WAL-only
rows with an empty `job` table). A successful snapshot archives settled
history; it does not clear jobs, consume inbox entries, or prove that an
interrupted response completed.

The exact OpenCode 2.3.0 V2-only CI job also runs the native smoke and the
offline native contracts for `tools/test_v2_lifecycle_podman.py` and the
exact-version global-vault helpers. The lifecycle host
runner separately covers encrypted full and portable restores, refusal before
destructive actions, retained sessions and reset on disposable rootless Podman
state. The CI job neither requires OpenCode 1.x nor exercises those live
Podman operations. It does not replace the pinned dual-version regressions or
the current-2.3 history job.

### Command help

Use offline CLI contract checks for the grouped `oc-box --help` index and
the exact option scope of each launcher command's `--help` page. Cover `-h`
and confirm dedicated pages such as `logs`, `web` and `secrets` retain their
scope. In particular, check that `start` help describes the journal writer
switches, while `logs` help describes the read-side `--journald` and omits
`--no-journald` and `--lifecycle-diagnostics`. Check option sets and groups
rather than snapshotting prose or line wrapping.

Check that help exits successfully without a workspace and does not resolve
selectors, acquire locks, access the vault, run Podman or native tools, or
read or change workspace or activity-journal state. Keep regression coverage
for implicit `start` and legacy selector-first invocations; help presentation
must not change parsing or defaults for ordinary commands.

### Quiet lifecycle and severity-based logs

Check the default launcher display on a PTY and through pipes or `TERM=dumb`.
TTY progress must be transient and cleared before native output, actionable
warnings or prompts; redirected output must have no routine per-stage
start/done lines. Keep native UI output unmodified. Check one final history
result for verified live or manual sync, recovered history, partial progress,
failure, an empty inventory and unverified cleanup. A recovered result must
carry the applicable interruption and unconfirmed-completion caution. Keep
retry guidance, raw nonzero client results, guarded routing and cleanup/exit
policy unchanged. Losing the optional diagnostics must not change the result.

Check `oc-box logs` as a read-only host command with no optional dashboard
imports, Podman/native calls or vault access. Cover the `info` threshold and
100-entry default, all four severity thresholds, selectors and their mutual
exclusion, limits after filtering, one validated JSON object per line, empty
JSON Lines output for no matches, and bounded follow across appends and
rotation. Missing or unsafe journal data must fail closed to empty. No reader
should create journal files or locks, emit its own activity events, or change
workspace or history state. Keep readable legacy rows without a severity,
malformed/oversized-row handling and private-file/rotation guards. Check that
the dashboard's oc-box source shares severity and message formatting but still
displays all levels, including `debug`, without expanding external log access.
Check the closed reason-code and metric-name lists and bounded integer metric
values. Use synthetic sentinels to verify that raw argv, errors, environment,
output, prompts, pairing links, credentials, clipboard contents and other
free-form text cannot enter the journal.

For the optional system journal backend, use mocked local sink and
`journalctl` checks. Mirroring defaults to off; cover saved `logs.journald`
booleans and one-invocation `--journald` and `--no-journald` overrides. Mirror
only after a validated private append succeeds. Check native priorities and
the fixed identifier and format marker, and verify that the mirror cannot
carry raw output, auth or secrets. A failed or unavailable sink must not roll
back a successful private append or change the lifecycle outcome. Ordinary
installs and default reads must not require systemd.

For `oc-box logs --journald`, check explicit backend selection independent of
the writer choice, current-user record filtering, validation of returned
activity records, and parity for severity thresholds, workspace selectors,
initial limit, follow and JSON Lines. An unavailable reader must give fixed
private-reader guidance without automatically falling back or exposing raw
errors. Mocked backend checks do not establish access, delivery, retention or
priority behavior on a real host journal.

Cover `--log-dir PATH` precedence over a valid saved `logs.directory` for
append and private reads, including `logs --path` and filtered reads. It must
not move old logs, change workspace selectors or retention, or create a
directory during policy loading or reads. Reject `logs --log-dir` with
`--journald`. Verify invalid saved log policy fails closed even when flags
override individual fields. Confirm no writes to saved config, no changes
to the process environment or to XDG and `OC_BOX_STATE_HOME` selection, and
that the former environment switches do not alter launcher or reader behavior.

For custom journals, inject a rotation-destination collision, a changed source,
and uncertain prune cleanup. Check that the existing destination is not
overwritten and that an unverified quarantine is not deleted or reused on
retry. Refuse pre-existing empty or malformed active data on repeated appends.
Keep default-journal partial-tail recovery and successful-append-only mirroring
covered separately. These fault tests do not establish protection from
arbitrary mutations by processes running as the same user.

Check lifecycle flag scope for `start` (including the implicit command),
`reset`, `stop`, `sync`, `terminal`, and `web`. Check `--lifecycle-diagnostics`
separately from the quiet default; it must not persist to later invocations.
Check bare `--verbose`, case-insensitive `ALL` and minimum-severity filtering
on the launcher console. Only allowlisted fields may appear in the added
activity diagnostics, even when the private journal is unavailable. Native import
notices stay short by default and disclose details only at informational
verbosity (`info`, `debug` or `ALL`); actionable refusals remain visible.
For the read-only `logs` command, cover `--verbose=ALL` with both source
choices and an explicit limit that overrides its larger initial view.
For owning preparation, verify selected subprocesses write neither stdout
nor stderr to the default console and that only bare `--verbose` or `ALL`
restores those streams. The task-specific start records are visible before
the subprocess exits, and results retain fixed failure categories and exit
codes. Exercise `logs --tail --follow`, filters, missing/unsafe destinations,
and untouched native/credential output. Keep preparation records in a
separate bounded ring so older launchers can still write to the normal log.
An interrupted final append must not replace an already-known child result.
`tools/demo_preparation_logs.py`
exercises these paths with disposable state and synthetic subprocesses.
For version-pinned scripted checks that parse legacy launcher output, pass
the flag only to the invocation under test. In the sharing host fixture that
is the owner expecting an explicit stop, solely for its legacy pending-history
line check; do not broaden diagnostics to other fixture invocations. Keep
historical reports, exact revision/native identities, original exits and
failed/refused checks intact. Local unit, process and native fixtures do not
establish a new external-host acceptance; do not replay retained fixtures for
this change.

### Experimental host clipboard attachment shortcut

`tests/test_host_clipboard.py` has seven focused groups: read-free helper
discovery and bounded PNG reads with a desktop-only environment; exact top-level
host shortcut framing and negative opaque/pasted-input cases; reuse of private
staging without automatic Send or re-enabling file drops; and CLI/client scope
plus distribution inclusion; bounded target negotiation without plain-text
requests/retries; copied PDF/image batches through real synthetic-file
capture guards; and default symlink following with strict opt-out, protected/
magic/special/hard-link denials and descriptor-bound target replacement checks.
No test reads the actual desktop clipboard or starts Podman.
Existing directly affected input/drop/completion checks remain separate.
The host-drop eligibility matrix accepts native 2.x minor/patch versions,
including 2.3.2 and synthetic future 2.x values, while retaining V1/malformed/
other-major and unsupported-terminal refusals. This is runtime eligibility, not
a claim that all native releases have been tested. Private transfer handshake
and framing checks remain authoritative; unrelated version-pinned fixtures and
history/recovery guards are not broadened by this feature.

The implementation deliberately leaves real Spectacle/Dolphin/Wayland/X11,
Konsole/Kitty shortcut reporting, native composer focus and image attachment
behavior to the operator's short [host check](host-clipboard-testing.md).
These local checks establish no live rootless-Podman or desktop acceptance.
Do not add a new broad harness or repeat unrelated release gates for this check.

Persistent clipboard policy additionally needs focused offline coverage for
absent-choice OFF, saved enable/disable inheritance, independent host-drop
policy, strict symlink-choice persistence (including disable/re-enable), and
read-free non-TTY `preferences` changes, including independent symlink-policy
saves while clipboard is off. Native startup symlink flags still require an
effective enabled clipboard preference. Cover private preference ownership,
locking, corrupt/unsafe refusals and host-only restore boundaries, plus saving
from an additional native TUI client without owner restart, unlock or config
refresh. Saved enable must skip unsupported terminals/native V1/missing desktop
helpers without clearing policy; explicit enable must retain strict preflight.
Web/terminal/manager/sync/info commands must ignore saved choices and reject
explicit startup flags. Neither startup nor preference changes may read the
clipboard or persist helper paths/content. These are policy regressions, not new
live desktop/native-composition or rootless-Podman acceptance. The historical
reports below predate persistent policy and retain their original scopes.

The relay recovery in `12bc3d5` adds offline framing and helper/transfer
failure cases in `tests/test_host_drop_input.py`, `tests/test_host_drop.py` and
`tests/test_host_clipboard.py`. Check that over-limit ordinary paste streams
byte-for-byte, holds at most a possible closing-delimiter prefix after overflow,
and does not authorize an import until new input after the exact valid end.
Malformed or incomplete framing must not look inside the paste for an import
trigger; reserved Kitty replies remain private. Verify that only a later
qualifying shortcut can recover from a first unexpected read with confirmed
helper reaping, a success does not replenish that allowance, and unverified
transfer stops all import routes in that client. Check fixed refusal categories
and bounded cleanup without restart, replay or automatic prompt admission.
The earlier human observations do not test this recovery behavior.

The follow-up input-framing regressions exercise pasted C0/DEL and CSI control
bytes across read splits. Check that these frames pass unchanged and cannot
grant while open, a later top-level shortcut can grant after a trusted end, and
remaining ambiguous frames still fail closed. The fixed exit notice
`input-framing-recovered` contains no frame data. These are offline checks, not
a reproduction or validation of the reported Yakuake failure.

The expanded relay recovery retains those regressions and covers runs of Escape,
common control keys after a pending Escape, recognized mouse/focus/key reports,
and opaque paste or control-string ends. A Ctrl+Y, Enter or Tab after repeated
Escape reproduced the same `escape-report-prefix` category as the operator's
report on `5b13a83`; the report did not identify which input actually arrived.
The relay forwards the recovery input without minting a clipboard or click
action, and a later independent gesture can operate in the same client. Keep OSC 72
reservation, ordinary Ctrl+V, callback refusal and cross-fragment tests. A
synthetic PTY acknowledgment checks that no host-open grant is sent before a
fresh click. `tests/smoke_file_preview_v2.py` additionally exercises native
hit-testing, repeated Escape followed by a fresh click, fresh file snapshots,
and unchanged web-link handling; `--private-mounts` adds the confined reader
with synthetic mount boundaries. Neither mode launches a real desktop viewer.

The owner reported opening a container Markdown draft in host Kate and
receiving changed content on a second click on experimental `3164f45`.
On `80f3e2b` they repeated Markdown, PNG, Ctrl+Y and a subsequent click twice
in the same Yakuake client, before reporting an Escape framing failure during
the HTML test. On `6f3e75a` they reported the workflow working repeatedly
after several Escapes and arrow keys. This is scoped operator evidence, not an
independently attested integrated-source pass. The final report did not
enumerate separate HTML sanitizer assertions, all formats, all terminals, or
the broader clipboard/drop and lifecycle checks. Preserve the earlier failure
reports; do not treat the later result as publication acceptance.

The owner's earlier "it worked" report covers the PNG workflow on native 2.3.2.
On diagnostic revision `af73035d848daa93ca832351564ba9635082568c`, the owner
reported `source-guard` for the original copied PDF, then a `[PDF 1]` attachment
after testing an ordinary copy. Both reports used the older Ctrl+Alt+Shift+V
shortcut. The Ctrl+Y binding is absent from the current documented OpenCode 2.x defaults
(checked 2026-11-18). On `f9be952e281007e345af35b0de38df6b037c709d`,
the owner confirmed Ctrl+Y delivery, identified a symlinked parent folder as
the cause of the source refusal, and reported success through the real folder
destination. That strict-source report does not establish linked-path support or
provider interpretation.

On 2026-11-18, after recreating the host test checkout using instructions pinned
to merged devel `c23897a10598c00797c4b4399a77ed6689d15eae`, the owner reported
"yes it worked" for copying a PDF through the linked folder and importing it
with Ctrl+Y under the default symlink policy. This is a scoped owner-reported
manual observation; source identity follows the provided commands, not an
independently collected host attestation. It does not separately verify the
strict opt-out, every supported format/terminal/platform, provider interpretation,
or exhaustive filesystem races. Earlier PNG and strict-source observations
retain their own revisions and scope.
Keep these scopes separate; no broader checklist or host pass is established
by the synthetic checks.

### Ctrl-click CLI and installation defaults

Verify the launcher options on implicit and explicit start, reset, and an
additional native TUI with an existing owner. The retired environment input
cannot opt in. Incompatible commands or explicit unsupported terminals refuse;
unsupported automatic clients use ordinary native behavior. Default-off and
explicit opt-out must not inspect preview mounts or supply private relay
metadata. Keep the existing fresh-click, immutable mount/descriptor, opener,
and no-write-back tests.

Stable native 2.x eligibility does not prove renderer compatibility. Run the
native Ctrl-click smoke against the retained 2.3 regression builds and current
stable 2.x. Check that launcher refusal reports a fixed reason before handoff,
without mount inspection on version or terminal refusal; a disabled preference
must remain quiet. The native fixture checks that a miss, an ungranted click and
output alone cannot start a transfer, even on a new minor release.
The launcher's PTY tests also check missing or overdue native hook startup,
invalid and duplicate announcements, ordinary output and exit preservation,
and the requirement for a new matched click after readiness.
The native smoke's `--hook-state missing` and `--hook-state incompatible`
cases verify that neither client receives a grant and that the incompatible
hook reports its refusal using the native TUI.
`--hook-state dragging` injects the observed native drag flag at the release
of a real assistant-link hit. A fresh host click can report that refusal but
cannot open the file, and neither an ungranted click nor an unlinked path
can trigger the warning.

Exercise verified fresh GitHub setup versus upgrade, early failure,
reinstallation, existing/symlinked state, and an unverified installation
record. Ordinary wheel or editable/source launches are not fresh-install
evidence. The new-install default must not be reactivated by restore; attaching
never changes another native client's choice.

Before publishing this follow-up, review an exact-source disposable Linux host
run with local rootless Podman and a compatible native OpenCode 2.x TUI in direct
Konsole/Yakuake. Check fresh flagless opening, an existing install remaining
off on upgrade, explicit enable and a second-client opt-out. Observe fresh
copies on two deliberate Ctrl-clicks and no open from unclicked output.
Record native, terminal, Podman, source and installation provenance. Keep
terminal/clipboard compatibility and ordinary owner exit/history distinct
from synthetic PTY and package fixture results. A disposable QEMU guest with
its own local rootless Podman and real Konsole can exercise this with scripted
desktop input; identify that scope separately from a physical-desktop run.
The older scoped Yakuake reports above are not evidence for the new installer
default.

### Experimental browser attachment adapter

`tests/test_web_attachments.py`, `tests/web_attachments_browser.mjs` and
`tests/web_attachments_shell_dom.mjs` cover
bodyless authenticated local resource routes, Host/Origin/Fetch Metadata/CSRF
refusals, source-version preflight, file-only standard DOM forwarding, native
path-mention defaults, media signatures, byte/count bounds, route changes and
no automatic prompt admission. The DOM fixture executes the actual common-shell
script and the attachment addon together, with one iframe/four tabs, trusted
input, same-target forwarding, per-document interceptors and selection-generation
refusals for same-ID events, explicit/observed SPA away-and-back changes, metadata
refresh, document loads and URLs changed before public verification completes.
These fixtures use synthetic browser objects, not a real browser compositor or
Spectacle drag protocol.

Run the separate provider-free native API contract on each supported release:

```sh
python3 -B tests/smoke_web_attachments_v2.py --opencode /path/to/opencode-v2
```

The original native source review used 2.2.6
(`dc2ffcf8bf26f9e669b6aa22b95f50e12be15e06`) and 2.3.0
(`a6b15d33daf6ab7d35569f6d711fdb630b9f04e7`). The fixture verifies real
native pairing/cookie authentication, bundled app assets, unchanged history/inbox
after shell/addon reads, and separate **explicit fixture-only** PNG/PDF byte
admissions with `resume=false`. The production adapter never calls that prompt
API. These checks do not prove browser/Spectacle/Wayland/X11/portal behavior,
native composer thumbnail admission, provider interpretation, live rootless
Podman isolation or new host acceptance. No native source/binary modifications
or private widget selector hooks are used by the adapter.

Before admitting another native release, also exercise unmodified native
browser assets in Chromium with the real pairing link and attachment addon:

```sh
uv run --no-project --with playwright python3 -B tests/smoke_web_attachments_browser.py \
  --opencode /path/to/clean/native --chromium /path/to/chromium
```

Verify a browser chooser and a trusted Chromium drop, exact native draft scope
across two workspace sessions, preserved text and media, and no prompt/inbox
mutation or execution before Send. Do not equate an OpenAPI or browser feature
check with draft semantics. Test an unlisted release by adding it in a
disposable source copy for the synthetic fixture before changing launcher
support. This is not a physical Spectacle drag, installed browser profile,
or live Podman acceptance test.

The compatibility donor `ff0e47e8376e6031fe4b907c165de00771a29663` documents
verified browser draft and no-automatic-Send behavior on native 2.2.6, 2.2.7,
2.3.0, 2.3.2, and 2.4.1. Native 2.4.0 passed the HTTP fixture with candidate
eligibility enabled, but the Chromium fixture did not verify completed PDF
staging. Its production refusal remains intentional; the HTTP result is not a
composer pass.
These results are for the donor's compatibility fixtures, not a pass of the
merged Studio browser flow or a new external-host Podman acceptance.

The common shell alone owns selection, tabstrip, metadata endpoints and its
single native iframe; attachments use only its frozen v1 API, events and optional
fixed script/controls slot. The independent wrapper is retired and tested as a
native 404, while addon script queries refuse. Default common mode imports no
addon. The native attachment fixture uses
a synthetic catalog of real native API sessions to isolate this HTTP composition;
the separate four-tab fixture below owns actual TUI membership verification.
Pinned baseline 2.2.6 CI invokes both fixtures. A keep-alive-capable HTTP client
also checks native-to-shell/addon-script navigation and preserved pairing
cookies under the response-close policy. Neither HTTP fixture is proof of
normal browser rendering or real browser file-drop admission.

**Retained failed attempt:** normal Chromium pairing on the original
`5c9fd883ded5d83c09a5d5e2842c739afd65647f` failed with a redirected wrapper 404:
the Gateway classified only the first request of a persistent connection.
That failure is retained, and a fresh-cookie diagnostic is not a normal-flow
pass. The separately reviewed Gateway response/connection repair
`0f1a366996833c65f56fbe5077159daf452790f2` is now merged without a duplicate
transport implementation. Subsequent normal-browser checks on the final composed
source are source-qualified below; this integration does not claim a completed
final combined-browser gate.

The later explicit-selection Chromium composition on `eff0c60` passed 69 checks
per native version, including real File PNG/PDF drafts and activated stock
service workers on 2.2.6/2.3.0. Integrated real-helper attempts on `871202a`
established automatic non-first owner selection and all four actual ordered
roots, but remained partial. The subsequent `d3b6fe9` private-controller timeout
persisted no phase results. No result from those sources is promoted to a final
combined-browser pass. Further private harness repair was stopped;
experimental development publication retains this explicit gap. See the
[release validation limitations](releases/1.7.md#attachment-and-web-validation-limitations).

### Open TUI sessions in the web interface

`tests/test_web_sessions.py` checks four-session membership, complete paginated
catalogs, native tab order, idle versus active semantics, restored launch scopes,
global/cwd scope, already-open archived/moved/worktree/child IDs, unopened-history
and foreign-service exclusions, identity-matching native GETs, empty/missing/
invalid state, owning-web selection, HTML escaping, native service-worker routing
boundaries and authenticated relay behavior. Native port-scoped cookies and
Origin checks remain authoritative. Empty/missing and authenticated error pages
retain explicit native Home navigation without invented session rows. Automatic
resume/history policy is separate.

Run `python3 -B tests/smoke_web_sessions_v2.py --opencode /path/to/opencode`
separately with native 2.2.6 and 2.3.0. It opens four tabs using native PTYs in
fresh synthetic stores, including a real Git worktree, a native-moved root and
a native-archived root, then exercises the production index with real native
HTTP pairing, session GETs and public web-shell routes. Only the outer Podman
exec boundary is replaced with local processes. Fixture state is retained under
a fresh temporary directory, including on failure. This does not render a
browser, certify browser-local tab seeding (which is not implemented), or
establish external-host/rootless-Podman acceptance.

The script first checks fresh, unbound HTTP pairing into an empty chooser and
its explicit native Home link, with unchanged missing TUI state and empty session
inventory. Use `--home-only` to run just this bounded read-only navigation check
without a TUI or session create/open/prompt mutations. Native Home web-shell
availability is not rendered Home chat or installed-service-worker acceptance.
The existing V2 CI job runs the default combined script against pinned 2.2.6;
both source/package and distro workflows also trigger on `fix/**` branches.

`tests/test_workspace_service.py` additionally checks bounded native response
headers, retained interim replies, response-close-driven redirect/cookie and
return navigation, HTTP 204/HEAD, binary uploads/downloads, streaming chunked SSE
and byte-exact bidirectional 101 upgrades. Only headers are mediated, never full
response bodies. The native `--home-only` mode uses a keep-alive-capable client
for pairing and repeated native/host navigation; request-side close alone is not
its oracle. These fixtures do not establish the composed attachment feature's
real-browser pairing pass. Retain the earlier Chromium native-404 evidence until
the final composed source is separately tested; a fresh-cookie workaround was
diagnostic, not acceptance of the original flow.

The common-shell successor requires new-source checks; earlier chooser/transport
passes are not promoted into UI acceptance. `tests/web_sessions_dom.mjs` executes
the editable browser JS in a bounded DOM fixture: four actual membership buttons,
selection/ARIA/query changes, one retained iframe/draft on refresh, same-private-
prefix SPA navigation verified by GET, foreign/Home/draft unbinding, safe text
rendering and non-overlapping metadata requests. This does not inspect native
DOM/stores or establish rendered native composer/focus acceptance.
The native web script checks the new shell bootstrap, single iframe, authenticated
assets/catalog/selection endpoints and explicit selection against both pinned
native versions, retaining cross-directory/archive and unchanged-history checks.

### Reviewed 5573965 candidate evidence

The following is a review of retained evidence for clean
`557396562b1b835d15ffcf152428762dc27d7ecd`, `1.7.110+g5573965`, count 139.
It is **not a new execution or exact-source acceptance of its channel-integrated
successor**. The owner now approves experimental `devel`/`.dev0` publication with
the incomplete UI scope disclosed, leaving stable v1.6.94/master unchanged.
That decision does not relabel any unfinished observation passed.

| Earlier-source automation | Reviewed result |
| --- | --- |
| [Source/native CI](https://github.com/groopcd/oc-box/actions/runs/43282466795) | 1,642 tests per Python 3.10/3.13 engine, zero CI skips; all 12 native scripts. |
| [Distro CI](https://github.com/groopcd/oc-box/actions/runs/43282466720) | Seven distro jobs passed. |
| [Build-only CI](https://github.com/groopcd/oc-box/actions/runs/43282469573) | Passed; publishing skipped. |
| Local suites | 1,642 tests each with two documented environment skips each, unlike CI. |
| Distribution audit | 60 wheel Git payloads, 197 tracked sdist files and 71 RECORD hashes; source identities and licenses reviewed. |

Earlier build-only wheel SHA-256:
`95d66f0006f547bb6a4adf7d55bc757a0e2a5a86c39e5a0e6b812481947c68cd`;
sdist SHA-256:
`d795bc34e6bfe4390d8a1ca9220ed2940b77a785e0d34843036167f5b5d9e588`.
These identify the old `1.7.110` build-only files, not a future published build.
The historical local-native failure remains unresolved/preserved; later clean
native/CI passes do not retroactively diagnose it.

| Earlier-source external-host scope | Retained report SHA-256 |
| --- | --- |
| History/readiness: 42 checks | `e287598d7b5afdff5c95586587e2a83d0481da24e2e6892fe1eba22501d926b0` |
| Recovery snapshot: 12 checks | `4583a5888ce2dca7f7240cb66f7494db0da507744f1a93c27a08481998c6cf56` |
| Stop cancellation: six checks | `c6cd959ff23c72a6f9889b7f2e48ca89721cf01eedd674b5e9c52a4055050fe6` |
| Global vault: 13 stages | `fa12479de9b6adcc6f2a12639a94c045dfed9b6535823fdc9b12c5bb44dcb62d` |
| Sharing: 37 checks | `bc65ed9d76f296b284509ac1a8db266c954ea9d486cd5d24eeb038a8da190c6b` |
| V1/V2 CLI lifecycle: six stages, including nine superseded-history checks | `bf600842be50b517962d6879ff5f890b3513cee92a6ae20d3375d07859d2784f` |
| Lifecycle source/native/launch pins | `e59cc23946e82176ccc73ef94e72ddc5b91c14f8beaf4b796f7aaac05010f40f` |

History exits were `[0,0,0,1,0,0,0,0]`; recovery owners `[0,1]`, cancellation
stop `0`. Sharing included actual nested-mount checks and enforcing SELinux,
with source labels unchanged. Lifecycle completed once, with fixture/launcher/
process exits 0, three distinct workspace IDs and 18 synthetic provider requests.
It used private native **V1 1.19.5 / V2 2.2.6**, without replacing system OpenCode.
The separate CLI lifecycle gate is **complete and must not be replayed for
publication paperwork**. Its raw output was not retained (13,841 stderr bytes);
no zero-stderr or comprehensive cleanup-log review is claimed.

These are scripted synthetic external-host/rootless results, not interactive UI,
real-provider/real-project restore, native-job cleanup, exhaustive races,
network containment, security certification or native 2.3 acceptance. Stable
1.6.94 evidence does not accept the new manager/sharing line.

#### Retained UI observations and stopped scope

Genuine navigation, settings, clipboard, loading/focus/resize/native PTY,
session backup/fresh-ID restore and masked-password observations retain their
original scope and `NOT_AUTOMATICALLY_PASSED` qualification. Failed broader
fixtures are not recast as complete passes.

Full workspace backup Create/Inspect returned 0; authenticated backup review
verified source/archive equality and unrelated ciphertext preservation. The
expected Host whole-workspace-backup preflight refusal was genuinely observed.
**UI full restore returned 0 and reported a fresh stopped workspace, but its
later integrity verifier refused `manifest`.** That failed report's SHA-256 is
`22dfa5cea90821897e1a80a513bfaee0d3cc0de9fa66a974e6938105ae660af6`;
saved failed-state SHA-256 is
`de7e370f7370630967be1eb46005793e1a08d0b554122c6d35b15ba51c87be49`.
Its empty `checks` object records no first failing subcomparison.

Production deliberately makes extraction roots 0700 and clears special bits
from non-symlink children. A subsequent metadata-only observation found source
home 0755 versus restored home 0700 with unchanged host UID/GID; both project
roots were 0700. This confirms an intentional transformation, **not all-entry
equality or the sole/first cause of failure**. Full restored integrity,
preservation and fresh-identity oracle completion remain unaccepted.

The portable UI Create/Inspect/restore sequence was **not armed**. Remaining
three-scope Storage observations, designated ciphertext deletion, original
synthetic source removal with project/history/retired-provenance/other-ciphertext/
both-restores preservation checks, the final journal/preservation attestation and
latest manager Quit confirmation remain unperformed or unaccepted. No current
resource, shutdown or lease-cleanup state is certified.

The owner stopped that sequence and explicitly selected experimental development
publication with these limitations. Do not resume its stores, replay completed
gates, decrypt evidence or treat publication as a waiver/pass. Preserve every
failed attempt and encrypted archive. Stable promotion remains gated separately.

#### Channel-successor validation boundary

The approved port changes install/update/version/publication handling and the
upgrade CLI path, not core workspace, credential, history, sharing or dashboard
behavior. Review unchanged core functions/modules and schema/provenance
separately from changed entrypoint paths; do not claim all runtime files or a
full old donor map are identical. Relevant new-source source/package/native CI,
guarded build-only distribution checks and independent review remain required,
especially for channel resolution, ownership delegation, previews, extras,
downgrade warnings and the `.dev0`/tag/branch/artifact identity chain.

No supported-version, protected-data, secret-lifetime or ownership regression is
retired to reduce testing. A new source hash and an experimental label do not
become an exact-source host pass; earlier results retain the hashes above.
Do not schedule needless broad host retesting for channel-only paperwork.
Future runtime changes/stable promotion still need applicable clean-source
evidence. See [the channel guide](update-channels.md) and
[publication checklist](../CONTRIBUTING.md#release-gate-and-publication-checklist).

### Experimental temporary host-file tools

`oc-box share PATH` uses native V2 experimental client-MCP, not a filesystem
mount or periodic snapshot. Keep its validation separate from TUI acceptance.
The feature branch is not publication or live host evidence.

```sh
uv run --no-project --with-editable . --with cryptography python -B -m unittest discover -s tests -p 'test_sharing*.py' -v
python3 -B tests/smoke_sharing_v2.py --opencode /path/to/opencode-v2
```

Unit/real-local-process checks must exercise live edits, child atomic replacement,
new/hidden files, root replacement/removal/close refusal, protected source roots
and ancestors/descendants, symlink/magic-link/traversal/hardlink/special-file
refusals, no-mount-crossing resolution without fallback, bounded text/image reads
and directories, malformed protocol/arguments, private-error suppression,
noninteractive acknowledgement and cancellation before backend work. Check
repeated reads, image MIME/content round-trips, no source modification, malformed
or oversized pipe traffic, receiver backpressure and startup disconnect/timeout.

Native offline checks require a private synthetic V2 server with no inherited
config, saved credentials, provider inference or normal shared service. Verify
real client-MCP handshake/catalog, actual text/image tool results, live reads,
unrelated MCP/config preservation, and tool removal/disconnect on close/reload.
Record the native version; an unsupported experimental API must refuse without
writing configuration. These checks do not prove host file/container isolation.

For relevant runtime changes or stable promotion, run on the **host**, outside
this container, using a clean
source-pinned candidate and synthetic files only. Check a running workspace can
share outside-root text and screenshots with no reset, host listener, mount,
credential refresh or saved MCP entry; ordinary container file/shell access must
still fail. Verify read-only/source preservation, scoped traversal/symlink/mount
denials, native permission denials, disclosure confirmation, two simultaneous
shares/workspaces, owner waiting for the explicit share lease, Ctrl-C and abnormal
host-client termination, explicit workspace stop, native reload, no automatic
regrant after resume/reset/restore, and backup exclusion of external source trees
while acknowledging retained tool results/history. Verify on an enforcing SELinux
host without relabeling original sources or disabling labeling. Preserve failed
reports and do not infer acceptance from unit/native or earlier TUI/vault passes.

### Cooperative native TUI selection reports

`tests/test_ui_state.py` and `tests/ui_state_fixture.mjs` cover the public-SDK
reporter, bounded server memory and stdlib host reader. Keep exact routed children
separate from ordered root tabs, verify native ancestry in the same private
service and reread the snapshot after verification. Test owner preference while
blurred, alternate ambiguity, initially unknown focus, heartbeat/release,
reload/epoch changes, expiry and replay after both ordinary and release expiry.
Registration capacity must refuse new IDs rather than evict sequence history.
Informational commands stay stdlib-only; attach must not install a helper,
refresh credentials/configuration or infer focus from saved tab order/activity.
Exercise both the live reporting and saved-membership contracts on the latest
stable OpenCode 2.x in CI, while retaining the original native regression.
Unit matrices cover eligible stable patch/minor versions, refusal of
noncanonical identities and incompatible protocol/state, and unchanged
automatic owner session selection. Eligibility to check a contract is not
confirmation that every 2.x release implements it.

Run the bounded provider-free native fixture separately against retained and
current unmodified supported V2 binaries:

```sh
python3 -B tests/smoke_ui_state_v2.py --opencode /path/to/opencode-v2
```

Its separate fixture-only public-SDK control plugin may navigate actual native
routes/tabs and exit the synthetic TUI; it must not replace report production,
fake native readiness or select through prompt/session-move APIs. Check already
read/no-idle route changes, exact child/root relationships, full ordered tabs,
pause/expiry/recovery, clean shutdown and unchanged native transcripts.

The composed browser gate additionally requires normal one-context pairing,
all actual open tabs visible around one native iframe, automatic selection from
real TUI reports, explicit override precedence, correct child/root display,
and no stale-owner fallback. Test PNG/PDF drafts before native Send, retained
drafts on metadata refresh, exact original-target binding and asynchronous
selection-change refusal. Record source/binary/browser identities and
service-worker activation separately; HTTP/Node checks alone are not browser
acceptance. Synthetic native/browser checks are not physical desktop gestures,
rootless-Podman lifecycle, real-provider or security acceptance.

### Experimental host-image attachments

`oc-box attach PATH --session ID` sends a host-authorized native image user
message. `oc-box attach --broker --session ID` instead exposes foreground
`request_image` proposals and single-use `read_attachment(id)` snapshots over
the existing private native client-MCP transport. The host must independently
type `attach` for every broker proposal. This feature is **not** transparent
native drop/paste interception and adds no container-native code/plugin
relocation. Its validation is separate from live `share` reads and the dashboard;
no earlier exact-source host acceptance establishes an attachment pass.

The following are required checks, **not a report that they have all passed**.
Do not run host lifecycle/Podman fixtures inside oc-box or against real
screenshots, projects or credentials.

**Unit/contract and real-local-process checks.** Cover required `--session`,
direct PATH versus pathless `--broker`, exact native directory validation and
refusal of observed archived/staged-revert targets. Both modes must require an
existing verified V2 owner on exactly 2.2.6/2.2.7/2.3.0, without starts,
auth/config refresh or vault unlock. Preserve parser/completion/manual and
installed-runtime coverage. Direct mode must use the default text
`Please examine this host image.`, allow an explicit text override and warn
before typed host consent or explicit noninteractive acknowledgement.
Verify native queueing with `resume: false` by default and `resume: true` only
with `--send`; do not test or document this as a draft chip or stopped-inference
guarantee.

Exercise independent host approval for every broker proposal, denial/cancellation,
and refusal of broker PATH/`--text`/`--send`/`--acknowledge-risk`. Bind opaque
expiring IDs to verified native caller-session `_meta`, reject missing/forged or
wrong-session context, expiry and reuse, and deliver captured bytes only once
without reopening the path. Cover parallel requests, bounded retained grants and
close/reload/disconnect/cancellation while awaiting host consent. Verify the
catalog contains only `request_image` and `read_attachment`, never
`list_directory`, arbitrary text or live file reads.

Reuse sharing path/content guards: protected roots/ancestors/descendants,
symlink/magic-link/hardlink/special-file and unsafe-change refusals, one regular
PNG/JPEG/GIF/WebP snapshot up to 8 MiB, unchanged source data, and no project
staging, native argv content/path leakage, raw diagnostic leakage or activity-log
retention. Native history/tool/provider retention is a separate disclosure and
must not be represented as prevented by these checks.

**Native offline integration.** Use unmodified supported V2 binaries with private
synthetic state/configuration and a synthetic local image. Check direct native
message submission, queue/resume values, actual native image normalization and
stored-message retrieval without assuming encoded-byte equality after native
processing. Exercise real client-MCP registration/catalog, request context,
host-approval handoff, returned image content, wrong-session isolation and
single-use/expiry. Preserve unrelated MCP/configuration and verify tool removal
on close/reload. Record each exact binary/source identity and whether a synthetic
provider was used; no normal host service, inherited credentials or real-provider
inference belongs in the fixture. Native/process results are not rootless-Podman,
desktop drag/drop or interactive host approval acceptance.

The bounded `tests/smoke_attachments_v2.py` fixture exercises the production
broker/protocol/WebSocket pump and stdin-only admission helper. Run it separately
with the retained baseline and current development native binaries:

```sh
python3 -B tests/smoke_attachments_v2.py --opencode /path/to/opencode-2.2.6
python3 -B tests/smoke_attachments_v2.py --opencode /path/to/opencode-2.3.0
python3 -B tests/smoke_attachments_v2.py --opencode /path/to/current-stable-opencode-2
```

Keep the native startup/broker and direct-queue checks separate. The broker
must complete initialize and tool-catalog negotiation with the existing
authenticated owner before exposing any grants. Do not treat API schemas as
proof that a new release will honor the direct queue/resume semantics.

Its approval callback is synthetic. The separate real-PTY approval unit cases
do not turn that callback into an interactive desktop/Podman acceptance pass.

**Focused external-host acceptance remains required.** On a clean source-pinned
candidate and synthetic workspace/image, start a broker in a second real host
terminal. Paste/drop only the screenshot path in the container native TUI and
explicitly ask for `request_image` followed by `read_attachment`. Observe that
the host separately confirms every file and that ordinary native container
file/shell access still cannot resolve the outside path. Check direct attach
with and without `--send`, explicit target/directory guards, archived/revert
refusal, source preservation, no mounts/listeners/staging/config or credential
refresh, owner waiting for the visible broker lease, Ctrl-C/client loss/native
reload/workspace stop, and no automatic regrant after a later launch. Inspect
native retention honestly: delivery/normalization and already disclosed copies
persist after expiry/close. Keep failed evidence and existing UI acceptance
limitations visible. No real screenshot, provider inference, security
certification or transparent native autoattachment claim follows from this gate.

### Experimental transparent native TUI host drops

Host-drop importing is a default-on per-workspace native TUI input bridge,
separate from the
per-image `attach --broker` confirmation workflow, browser uploads and web
session-routing fixes. Its supported native versions are 2.2.6/2.2.7/2.3.0.
The experimental terminal profiles are direct Konsole and Kitty, chosen by
unambiguous host compatibility markers, not authenticated emulator identity.
Unknown/ambiguous, multiplexer and SSH setups skip automatic importing; explicit
enable refuses them. A complete qualifying pathname paste anywhere in an
importing-enabled TUI authorizes capture without per-file prompts; neither a genuine desktop drag
gesture nor composer focus is established. Konsole is pathname-paste-only and
leaves child output unchanged. Only the Kitty profile probes/enables structured
OSC 72 DnD after actual capability detection; graphics support alone is not DnD.

The following are required checks, **not an assertion that the feature or a
desktop/rootless-Podman gate has passed**. Do not replay older accepted fixtures
or use real screenshots, documents, workspaces, credentials or provider accounts.
Freeze the integrated source before shared-checkout tests; record exact source,
working-tree state and native binary identities.

**Unit, protocol and process contracts.** Exercise the normal imports in
`tests/test_host_drop.py`, `tests/test_host_drop_input.py` and
`tests/test_host_drop_io.py`, `tests/test_workspace_preferences.py` and
`tests/test_host_drop_preferences.py`. Cover default-on supported clients,
saved opt-out/enable, read-only preference display, corrupt/unsafe policy refusal,
unsupported-client automatic fallback versus strict explicit enable, and start/reset,
plus an additional start TUI client that joins the existing owner without
restart, credential/config refresh or new ownership. Never activate on V1 or
unsupported native versions. Reject startup flags on web, terminal/maintenance,
the dashboard, sync, informational and other commands; require an actual
interactive TTY without TMUX/STY/SSH for importing.
Keep parser, completion, manuals, installed-runtime and wheel/sdist membership
contracts together.

Check fragmented and complete pathname/structured-drop frames, one to four
absolute quoted/escaped Linux paths, and rejection of prose, newlines,
traversal, control characters, file-URI text expansion and unsupported formats.
Opaque OSC/DCS and other terminal responses must not become host-path
authorization; exercise UTF-8/C1 boundaries and bounded partial-input recovery.
Konsole must emit no private OSC 72 probes/registration and preserve all child
output. Only the Kitty profile mediates its C0 OSC 72 wire commands; include C1
wrappers hiding C0 commands, numeric aliases, malformed escape recovery and
nonjoining output neutralization. Verify final-emitted framing boundaries and
pre-threshold fail-closed raw-wire limits while retaining valid graphics APC_G,
keyboard CSI and supported clipboard traffic. Input C1 opacity remains separate;
do not claim a generic C1-output or universal terminal sandbox.

Reuse production sharing pinning/protected-path/link/special-file/change
refusals. Verify PNG/JPEG/GIF/WebP/PDF extension/signature agreement, including
PDF-prefix mismatches and DOCX refusal. Exercise 8 MiB/file, 16 MiB/batch and
non-reusable 32-file/64 MiB client quotas without large unbounded fixtures.
Check private non-PTY stdin delivery, no captured bytes/source path in argv/env/
shell/project files, and no model/MCP path-request authority or per-file prompts.
The isolated-source helper must create an exclusive 0700 pinned container root,
0600 generated files, sanitized errors and one bounded close acknowledgement.
Exercise EOF/explicit close, overwrite/symlink/root-substitution and ownership/
mode refusals, partial writes and cleanup of recorded identities only. Preserve
unrelated files; do not add background or restart cleanup.

**Native offline regression, including PDF.** The focused
`tests/smoke_host_drop_v2.py` fixture must use unmodified native binaries with
private synthetic configuration, history, image and PDF bytes. Keep the older
regressions and check the current stable V2 release in CI without a fixture
minor-version allowlist:

```sh
python3 -B tests/smoke_host_drop_v2.py --opencode /path/to/opencode-2.2.6
python3 -B tests/smoke_host_drop_v2.py --opencode /path/to/opencode-2.3.0
python3 -B tests/smoke_host_drop_v2.py --opencode /path/to/current-stable-opencode-2
```

Verify actual native attachment admission/serialization of PDF as
`application/pdf`, not only a Python prefix test or a source-reading claim.
Selected model/provider PDF support is separate; no DOCX reader/converter or
real-provider inference is established by this fixture. Cover container-path
rewriting fed as native paste, without an appended Enter or prompt API
submission. Native UI composition/preview and the decision to send remain native;
`--image-preview` is an optional existing owning-launch visual overlay, not
host-file authorization or proof of rendering. No offline/native result alone
is a desktop gesture, focused-composer or rootless-Podman pass.

**Private X11 desktop regression.** With Xvfb, software OpenGL, Kitty or Konsole,
`xclip`, `xdotool`, and Pillow with XCB support, run:

```sh
uv run --no-project --with pillow python3 -B tests/smoke_image_preview_x11_v2.py --opencode /path/to/clean/native
uv run --no-project --with pillow python3 -B tests/smoke_host_clipboard_x11_v2.py --opencode /path/to/clean/native --terminal kitty --verify-prompt-pixels
```

The preview smoke checks a synthetic transcript PNG's color pixels in memory
with the override on and off. The clipboard smoke checks real X11 typed
selection, a Ctrl+Y key event, PNG and followed URI-list symlink imports, strict
symlink refusal visible in the composer, unchanged selections, and a separate
explicit Send. The optional pixel assertion independently checks that the
composer renders the PNG before Send. Only
the Podman exec boundary for the private helper is replaced with local
execution. Use `--clipboard-backend wayland` with Weston 16 and `wl-clipboard`
to check the actual Wayland clipboard backend in a private headless compositor;
the terminal and scripted key input still use X11. These are isolated-desktop
checks, not physical hardware/input, a Wayland terminal session, or Podman
acceptance. Neither reads the user's clipboard nor saves screenshots.
Native current-stable CI separately checks that the image preview
overlay is effective without persisting it in native preferences.

**Focused external-host observations remain outstanding.** On clean pinned
source with disposable synthetic data, check Konsole's default direct drops
with bare names/trailing spaces, quoting/escaping, batch handling, denied/protected inputs and
non-composer focus. Verify that optional drop-menu Paste Location stays unframed
and is not imported. Check genuine structured DnD only on a supported Kitty
profile exposing that capability; record unsupported capability honestly. Observe native draft/
attachment preview, confirm no automatic Enter/submission, and send explicitly.
Exercise both a new owner and an additional opted-in TUI, ordinary unflagged TUI
behavior, resize/graphics/keyboard preservation, client detach/loss, owner
waiting and normal exit. Verify source preservation, private container staging,
best-effort recorded-file cleanup and honest retained-data reporting after
interruption. Container root/same-user agents can read staged data; references,
history, copies and backups may retain bytes even after cleanup.

These are not a malware/content scan, terminal-version certification, blanket
security guarantee or acceptance of the separate web features. Keep the existing
UI limitations and all earlier source-qualified evidence unchanged.

### Experimental workspace manager

The [Textual manager](tui.md) adds a separate validation surface from the native
OpenCode conversation TUI. Its development publication is explicitly
**experimental, not stable manager acceptance**; see the
[retained evidence and incomplete UI scope](#reviewed-5573965-candidate-evidence).
The checks below remain validation/stable-promotion requirements, not a record
that every observation was executed or passed, nor a request to resume stopped
fixtures. Demo, headless
behavior, real native APIs and operator-observed Podman/terminal behavior
provide different evidence; none may be substituted for the next layer.

**Stdlib unit/contract layer.** Keep normal non-TUI imports and commands usable
without Textual/pyte. Cover CLI selectors, optional-dependency diagnostics,
host/regular-user/TTY guards, read-only action refusals and synthetic-only demo
isolation. `tests/test_tui_sessions.py` exercises the transport-free session
domain; keep that separate from proof of actual native API behavior. Required
cases include complete pagination across projects/children/archives, invalid or
partial inventories, verified controller-archive mapping, identity-bound private
favorites, recursive deletion scope and typed confirmation, the 50-session
dashboard cascade-review limit, active/unconsumed or unknown-activity refusal,
process-local race warnings and independent source/copy effects. No inferred V1
archive API or permanent mirror suppression is allowed.

Generated activity-journal tests must check private ownership/modes, unsafe-path
refusals, fixed categories/allowlisted metadata, four 256 KiB files at
`log/activity.jsonl` and rotations `.1` through `.3`, and readers that never
create files. Supply synthetic credential/error/argv/env/transcript sentinels
and verify they cannot enter generated entries. Podman output is a distinct
opt-in: require a verified owned selected container, tail 500, 1 MiB and
10-second bounds, memory-only retention and best-effort common-pattern
redaction. Tests of known patterns are not proof that arbitrary secrets are
removed. This stage has no native OpenCode logfile reader.

**Optional headless UI layer.** Exercise all seven sections and `1`–`7`, `r`, `q`,
`Ctrl+L`, `s`/`F2` and focused-terminal routing with Textual's headless behavior
tools. Check the wide rail and compact header section picker without hiding log
search. Search must stay visible and case-insensitive/literal across tabs, source
changes, follow and pause. Check the ordinary 2,000-entry buffer, the private
1,000-entry read bound and the separate explicit mirror snapshot of up to
1,000 entries.
Verify that input-clear controls are double-width error-colored crosses in
reserved padding, excluded from copied field contents. Both icon cells must
clear exactly once; crops through either half must preserve terminal-cell
bounds and not damage Unicode input rendering. Clearing log search in
wide and one-row compact headers restores the source-scoped entry count, never
deletes entries or triggers backend/native work. Distinguish unfiltered entries
from filtered matches, including zero and singular counts. A selected log
viewport stays frozen while its fixed-size count updates without relayout or
extra searches for unchanged same-source snapshots. Switch source while RAW
or table output is selected; stale rows must not be copied or opened as
details for the new source. Cover successful empty private reads, a changed
directory with a read in flight while follow is paused, an unavailable Podman
target, and a private tail refilled after eviction without refill oscillation.

Verify bounded host resource cards and the selected-workspace inspector,
compact resource-strip rendering and short fixed 0–100% CPU/RAM
histories, not auto-scaled sparklines. Use synthetic readings, including
workspace CPU above 100% (100% = one core), observed quota normalization, unknown/
unlimited capacity, unavailable readings and unchanged raw totals/data. Check
that full details/mounts remain expandable and initially collapsed. Include no
current workspace: System and monitoring settings must remain usable without
fabricating workspace readings. Include 80×24, 100×30, 140×44 and wide layouts,
header/context selection from every section, empty/partial inventories, selected
identity/viewport preservation and no extra backend work from navigation or
filtering. Card defaults must retain selectable built-in themes.
Queued/detached picker events must be ignored before root/private reset queries;
valid private-shield changes reset to the admitted context without actions.
Oversubscribed quota captions must retain actual finite percentages while bars
saturate visually; unknown/unlimited values must never become false utilization.
The activity card must use already-retained oc-box categories only and respect
selection/clipboard/privacy boundaries. Existing secret and native-terminal
fences are not a cosmetic implementation detail to replace.

Check cheap batched Status and followed-log refresh at every whole-number
interval from 1 through 60 seconds, default 1 second, and auto-refresh on/off.
The custom Monitoring field must reject blank, fractional, nonnumeric and
out-of-range drafts without applying other edits; Reset stages 1 and Revert
discards it. Include custom values such as 7, 17 and 59. Use controlled
slow queries to verify no overlapping or queued refreshes; cadence is best-effort,
not an exact wall-clock guarantee. Native session inventories remain manual
Connect/refresh, and expensive allocated storage scans remain manual and bound
to the selected workspace. Settings may change dashboard refresh, never workspace
limits.

Exercise the inline Settings section and `s`/`F2` navigation, categorized
Monitoring, Display, Sessions, Logs, Resources and Safety/terminals topics, built-in
Textual themes and details visibility. Revert/Escape must discard pending edits;
Apply must accept only allowlisted session-only preferences without persistence
or journal writes. Reset to defaults must stage the full factory preference set
without changing active values until Apply; Revert/Escape must discard that staged
reset without altering history, favorites or retained logs. Session scope/filter
changes must not connect or refresh native inventories; log source changes must
preserve explicit opt-in guards.
Dirty drafts must survive repeated entry, background status updates and context
selection without changing the applied polling cadence. Do not retain duplicate
or hidden monitoring controls. Compact switches must remain clickable and
Space-operable with visible focus and spatial on/off state at 80×24 and 140×44.
Log search must never persist or enter the journal. Resources and CLI-mode safety
information stay read-only, and no preference may weaken `--read-only` or change
OpenCode `cli.json`/host settings. Settings shortcuts must not steal focused text
input or native-shell keys, including `s` and `F2`; detach with `Ctrl-]` before
dashboard shortcuts. Keep log search available from every tab without stealing
native terminal focus on refresh.

For display regressions, run the synthetic workload in
`tools/benchmark_tui_display.py` with the optional TUI dependencies. It uses
32 synthetic workspaces, 1,000 session-metadata records and 2,000 log entries,
not actual stores or Podman. Check deterministic work counts rather than tight
wall-clock thresholds: a typing burst should coalesce to one final filter;
unchanged session/log snapshots should not clear or repopulate the view.
Verify immediate text/caret editing, final-filter delivery after the short
debounce, stale callback rejection, same-store cursor/scroll preservation,
different-store selection separation, and favorites I/O outside rendering.
All metric samples and fixed-scale histories must remain represented, and a
redraw must never become a clipboard gesture. Local benchmark timings do not
establish real-terminal or host acceptance.

Background Status/log/storage updates must leave Sessions rows, selection,
cursor and scroll position unchanged. Check this with enough rows to scroll,
not only a single-page table. Inventories update only on explicit Connect/manual
refresh or relevant on-demand filters/actions; a static snapshot is not a live
subscription. Demo refresh must keep its fixed synthetic readings unchanged.

Demo must use synthetic data without real services/registry/log reads or actions;
instrument those boundaries to fail if touched. There must be no implicit
start/reset/sync/unlock, `podman update`, resource-control RPC, CPU/RAM/storage edit,
storage resize/quota or store move.

Exercise the optional pyte view with bounded synthetic PTY bytes, resize/input
routing, explicit create/takeover confirmation, session membership checks,
`Ctrl-]` detach, worker cancellation and client-lease cleanup. These checks do
not establish complete xterm support; mouse/images/complex graphemes/extended
keyboard protocols are outside the initial emulator. Neither headless snapshots
nor the container-permitted demo are interactive host acceptance.

**Actual native V2 API layer.** Use unmodified native executables and private
synthetic databases/config, not normal host history or credentials. Verify the
real paginated global/project inventories including archived/child sessions,
native archive/unarchive, recursive deletion/refusal and empty-success CLI
responses as well as HTTP behavior. Exercise production discovery/connection
paths explicitly: opening the manager must not connect; explicit host Connect
may start the normal native host service, while a workspace connection must use
an existing private service and never redirect to host. Check that Disconnect
and TUI exit release only their client leases. Native API/CLI fixtures prove
their scoped behavior, not rootless containment, interactive rendering or a
cross-process delete transaction. V1 launcher/migration regressions remain
required, without claiming native V1 manager actions.

Keep native parent cascades distinct from archive provenance relationships:
delete previews include native descendants across project filters, while a
managed host copy with native children refuses deletion. Running PTYs, even idle
shells, must block deletion without being killed. Archive changes only the
selected native object; archived workspace roots remain synchronized but must
not become automatic resume targets.

Run the focused transport-free contracts and actual native CLI fixture separately:

```bash
uv run --no-project --with-editable '.[tui]' python -B -m unittest discover -s tests -p 'test_tui*.py' -v
python3 -B tests/smoke_tui_v2.py --opencode /path/to/opencode-v2
```

The native fixture uses 106 synthetic sessions, including two directories,
archived roots/children and a real `100 -> 6 -> 0` pagination chain. It checks
native PATCH/DELETE no-content transport, quiet-store refusals, scoped recursive
deletion and preservation of independent roots. It does not exercise the host
dashboard discovery path, real history, inference, Podman or interactive terminal
rendering. The UI tests also cover bounded favorites locking, malformed colors,
stale connections, typed deletion, source identity and searchable paused logs.

The Logs table and RAW view use synthetic records for header sorting, column
visibility, row inspection, source filtering and the bounded clipboard copy.
Check that view choices never mutate logging policy or implicitly read either
external source, and that switching between table and RAW does not reconstruct
unvalidated text. Exercise the 2,000-entry ordinary buffer independently of
the explicitly loaded mirror snapshot (up to 1,000 entries); the latter is
neither merged into ordinary sources nor refreshed by ordinary follow. Check
that table, RAW, count and clipboard actions stay consistent with the selected
source and filters when log policy or workspace context changes.
Host logging settings use a private temporary config to
exercise explicit saves, preservation of unrelated values, unsafe/stale-file
refusals and demo/read-only guards. System-journal tests in this suite use
injected reader results; they are not acceptance on a real host journal.

Run the encrypted session-history engine and actual native CLI-body transport
fixtures separately; both require native 2.2.6 and host age tools:

```bash
python3 -B tests/smoke_session_backups_v2.py --opencode /path/to/opencode-v2
python3 -B tests/smoke_session_backup_transport_v2.py --opencode /path/to/opencode-v2
```

They cover history above 4 MiB, encrypted envelopes, fresh IDs/cleared privileges,
source preservation and import verification. The transport fixture uses the real
production host/workspace stdin-import hooks and native CLI, but explicitly adapts
discovery/environment, workspace selection/lease and the Podman exec envelope to
a private local server. It does not establish normal-host discovery, actual
Podman stdin delivery or interactive TTY acceptance. The ordinary native CI job
keeps both fixtures enabled alongside V1/V2, plugin, vault and permission checks.

**Packaging layer.** Verify `oc-box[tui]` in isolated uv/pipx installations and
GitHub `--with-tui` both alone and with `--with-vault`. Check private per-release
dependencies, running-release pinning and retained opt-ins on GitHub update;
never install packages into system/user-site Python. Default wheel/CLI tests
must still run without the optional UI dependencies. Test help/argument handling
and normal CLI compatibility in those environments, not just imported metadata.

The build-only publication gate must check `--with-vault --with-tui`. Development
requires matching `.dev0` public/resource versions, `devel` source membership
and a GitHub prerelease, bound to the exact wheel/sdist and validated clean
identity. Preserve the guard preventing a GitHub prerelease from uploading the
ordinary final `1.7.N` version; manual `publish=true` cannot bypass it.
Verify existing release assets and transfer only the workflow's validated bytes.

**New clean-host gate before stable publication.** Freeze the fully integrated
candidate and record its full commit, clean state, native executable identities,
dependency versions and exact test scope. Use a regular unprivileged host TTY,
fresh synthetic projects/history/config and a verified isolated rootless Podman
store outside containers/sandboxes. Do not run these probes inside oc-box, use
real workspaces, or change the candidate while its tests run. Required manual
observations include:

- Seven-section navigation (`1`–`7`), responsive rail/header and bounded
  host resource cards/compact strip and selected-workspace inspector on
  small terminals, readable fixed-scale histories and expandable full details/
  mounts. Log search remains available while following/paused on every tab.
  Observe default one-second and selectable shared Status/followed-log refresh,
  best-effort slow-query behavior, and explicit bounded storage/log reads.
- All categorized preferences directly in Settings via `2` and `s`/`F2`,
  including with no workspace selected and no extra settings window. Check
  Revert/Escape discard, session-only Apply, staged Reset to defaults followed by
  both Revert and Apply, compact on/off switch click/keyboard behavior,
  theme/details display, no automatic session connection, unchanged host/
  OpenCode settings and no settings/search journal entries. Read-only mode cannot
  be weakened; there is no resource mutation path.
- No default service start; explicit native host Connect with its start warning;
  verified host-project archive mapping and unchanged workspace source copies
  after actions on synthetic host copies.
- Existing private V2 owner attachment, visible owner wait while the manager
  lease is held, Disconnect/quit lease release, and concurrent native TUI/web
  clients staying independent. No vault prompt, credential refresh or implicit
  ownership takeover on attach.
- Actual embedded native shell rendering/input/resize, explicit create/takeover,
  foreign-source refusal, Ctrl-] detach/reattach with retained shell state, and
  dashboard closure leaving the native PTY/other clients intact. Terminal focus
  must not turn ordinary shell keys, `s`, `F2` or `Ctrl+L` into global dashboard
  actions; detach before using dashboard shortcuts. Refresh must preserve native
  terminal focus, and settings shortcuts must leave focused text inputs alone.
- Guarded native V2 session archive/unarchive and recursive-delete refusal/
  confirmation on disposable sessions only; preserve process-local race warnings
  instead of claiming cross-process exclusivity. Check complete parent cascades
  across project filters, the 50-session review ceiling, managed-copy child
  refusal and running/idle-PTY deletion refusal. Verify an archived workspace
  root still syncs, is excluded from automatic resume, and remains available
  for an intentional unarchive. Keep independent workspace/host copies intact.
- A multi-page Sessions table retaining its cursor/selection/scroll during
  background Status/log/storage updates, with explicit manual inventory refresh.
  Delayed connections visibly show source/stage and an animated wait state;
  cancellation remains visible until its bounded call finishes, duplicate clicks
  do not silently disappear, and stale callbacks cannot clear another request.
  Fixed demo data must remain clearly synthetic, not a live host measurement.
- Actionable failure notifications plus persistent notices for every deletion
  refusal category, including managed native children, running shells, active
  work, stale reviews, unknown ownership/activity, read-only and incomplete data.
  Transport uncertainty must not claim nothing changed; successful deletion with
  a failed refresh must remain distinguishable. No raw private errors are shown.
- Storage metadata across host/selected/all scopes, canonical target binding,
  bounded nonblocking registration reads, typed one-workspace purge retaining
  project/history, and owner/lease/exposure refusal without forced cleanup.
  Explicit full/portable backup, inspection and new-directory restore through
  private in-dashboard masked editing backed by the actual child controlling
  TTY; no password can enter selectable Input/widget values/messages, UI
  selection/copy, protocol metadata, argv/env, plaintext files, logs or journals.
  Verify debug/developer/event-logging refusal, core-dump suppression, prompt
  readiness/echo fences, confirmation focus and queued-input isolation, Enter
  refusal on the next authenticated metadata review, global clipboard/help/
  palette/clear/navigation fences through child exit, and local focus traversal.
  Read-only/demo gates apply again
  at execution and source changes invalidate reviews.
- Encrypted session-tree backup/restore to host and existing workspace sources,
  including history above 4 MiB, fresh IDs, cleared privileges, verified import,
  retained source/independent histories, and honest partial-failure recovery.
  The dashboard does not delete backup files; even a stale review request must
  refuse without mutating the archive or its neighbors. No whole host database
  or shared-cache deletion, implicit owner launch or backup overwrite.
- Visible-text selection/copy across diagnostics, table/log rows, the embedded
  shell, graphs, control labels and modal reviews/settings, including wide/
  combining characters and scrolled views. Verify visible highlighting over
  colored logs/row cursors/meters and meaningful copied graph glyphs. Real
  mouse-release selection copies once, never during the drag; double/triple-click
  text and keyboard input selections copy too. Dragging a control/footer label
  must not activate it. Copy view remains optional, without exporting hidden/
  offscreen data. Programmatic selection, focus, refresh and container output
  never trigger copies. Host delivery depends on actual terminal OSC 52 support/
  permissions. Check desktop paste and the separate in-app Paste copied
  shortcut in text fields and a connected shell; preserve ordinary shell
  Ctrl+C/Ctrl+V, focus, UTF-8/
  framing/queue bounds, atomic size/capacity rejection, uncertainty-aware
  partial-transport failure notices, read-only refusals and no clipboard
  content in journals. Container output must not issue host clipboard writes.
- In-dashboard backup progress must identify real stages and report bounded
  measured ciphertext bytes during streaming, checksum and packing. Opaque
  stages show no guessed percentage or ETA; samples never delay authoritative
  prompts, cancellation or results. Inspect only after explicit action,
  route by bounded envelope structure outside the UI thread, retain editable fresh
  destination suggestions, and refuse authentication without trying another store.
  Cancel/Ctrl+C must latch one interrupt and wait for actual cleanup/exit, including
  parent/control EOF, prompt/review transitions and post-dispatch cancellation.
  Verify startup failures with no worker cannot abandon a child, nested handlers
  retain the latch, descriptors do not leak to exec helpers, next-prompt IDs are
  not clobbered, accepted cancellation cannot become ordinary success, and partial
  session-import IDs survive interruption in normal and recovery paths. Run these
  on fresh synthetic state; none of the earlier incomplete UI evidence is passed
  merely by completing headless/process/age tests.
- Private fixed-category journal creation, permissions and bounded rotation;
  synthetic argv/env/error/prompt/transcript sentinels must not enter generated
  entries. Verify readers do not create files, default log browsing does not
  fetch container output, and opt-in Podman logs stay owned, bounded and
  memory-only. Known-pattern redaction is not a guarantee of secret removal.
- An isolated non-editable launcher for managing oc-box's own synthetic checkout;
  an exposed editable launcher must still refuse rather than bypass APP/state
  trusted-path validation. Ordinary installed launchers for unrelated projects
  must continue to work.

For a future stable integrated manager candidate, require clean-source reports
from all three current host fixtures—`tools/test_session_sync_podman.py`,
`tools/test_global_vault_podman.py` and `tools/test_v2_podman.py`—on the same
candidate, in addition to the Textual/journal/archive-resume observations.
This is not a request to rerun the completed `5573965` CLI lifecycle gate or the
stopped dashboard fixtures for the approved channel-only development successor.
The focused runner now requires **42 named checks**: all 33 earlier checks plus
nine archive/resume regressions. Its ordered launcher exits must be
`[0, 0, 0, 1, 0, 0, 0, 0]`; the single `1` remains the deliberate pending-shell
refusal. The new stages archive every root while retaining sources/archives,
verify no automatic resume target, observe a fresh conversation on the next
ordinary owning start, unarchive a prior root and observe it selected on the
following start, with unchanged inference counts. A historical 33-check pass
does not establish these new behaviors.
Use the [source-pinned handoff](#next-migrated-history-release-candidate) below
and the [publication checklist](../CONTRIBUTING.md#release-gate-and-publication-checklist).
Repeat other relevant backup/ownership checks where affected. Keep reports
source-bound, retain failed evidence and encrypted artifacts, and record operator
observations separately rather than self-certifying them from scripts. Earlier stable host
reports and the old native terminal pass retain their original scope; they do
not validate the new dashboard, embedding or journal. No new host pass or stable
publication is recorded by adding this plan.

Fresh `tools/test_interactive_podman.py` fixtures now configure a source-bound
private native host-service port before any explicit host Connect. Native 2.2.6
rejects port zero as a saved service setting: the fixture reserves an ephemeral
IPv4 loopback port, sets that explicit number once, and records the selector.
Setup does not start the service. File-only validation accepts native's later
opaque authentication addition while preserving private modes and the exact port,
wrapper, profile and source bindings. The reservation closes after configuration;
a later bind race fails closed with retained evidence, never automatic retry,
default-port fallback or shutdown of an unknown listener. Old retained fixtures
are neither migrated nor replayed with this new helper. This is a fixture change,
not a production host-service discovery or permission-policy override.

### Optional social release announcements

`tests/test_release_announcements.py` exercises synthetic Buffer/GitHub responses,
stable/devel highlights, actual public-package identity handoff, default-off
behavior, credential-safe requests, durable pre-mutation claims, rerun receipts
and read-only reconciliation after uncertain outcomes. Existing release-channel
and artifact-validator regressions remain independent and unchanged.
`tests/test_release_card.py` checks deterministic bounded 1200×630 PNG rendering
with optional Pillow; source/publication CI installs it only in isolated test
tools, never as an ordinary oc-box dependency. These offline checks neither
connect a real profile nor certify LinkedIn delivery. See
[setup and limits](release-announcements.md); no live posting test is implied.

### Ubuntu CI package bootstrap

`tools/ci_ubuntu_apt.py` generates a temporary, CI-only APT configuration for
Ubuntu hosted-runner prerequisites and the Ubuntu 24.04 distro fixture. It uses
the independently hosted HTTPS Ubuntu archive at
`https://mirrors.edge.kernel.org/ubuntu`, including the release, updates,
backports and security suites. Package signatures still use the image's existing
Ubuntu archive keyring; TLS peer/hostname checks and repository freshness checks
stay enabled. Strict update failure and bounded retries/timeouts prevent a failed
fetch from silently reusing partial or stale indexes.

The helper does not install packages, rewrite `/etc/apt`, change DNS, introduce
signing keys, or change production dependency mappings. Runner installs pass its
configuration explicitly with `apt-get -c`. Only the Ubuntu test container
inherits `APT_CONFIG` for its initial Python install and subsequent real provider
and baseline commands; the other six distro paths are unchanged. A read-only bind
of the same generated directory carries the runner's public CA bundle into the
minimal Ubuntu image, allowing HTTPS before `ca-certificates` is installed.

The generated configuration contains only public repository/CA data. Workflows
place it under a fresh, traversable `/tmp` directory, with directories 0755 and
files 0644 so APT's `_apt` downloader sandbox can read it. Do not reuse a secret or
workspace directory for this purpose. Unit tests cover generation, refusal and
trust settings; the Ubuntu CI jobs must still demonstrate actual signed package
downloads and complete all tests. A successful configuration step alone is not
CI acceptance, and CI repair is not a new rootless-Podman host pass.

### V2 session-history synchronization

The investigations, original snapshot/backport descriptions and accepted reports
below retain their original source-specific nonzero outcome policy. The later
[focused shutdown-outcome policy](#focused-shutdown-outcome-policy-checks) changes
only the fully verified clean-client result; it does not rewrite historical exits,
pass failed gates or extend earlier acceptance to the new source.
The one-attempt automatic archival amendment below has separate preparation and
validation requirements; it does not extend historical acceptance to foreground
timeouts or ordinary live-sync failures.

#### Exit-timeout investigation (2026-11-12)

A synthetic native investigation reproduced a retained-job exit timeout on
unmodified OpenCode 2.2.6 and 2.2.7: reloading a location while a background shell
is held can leave zero foreground sessions and one cancelled shell job. Releasing
the shell did not consume the job or produce its completion notice; a real
30-second production waiter still refused. Reopening could hydrate the row as
running, while checked native export remained acceptable. Explicit sync bypasses
the original owner's global readiness wait, so its success does not establish
that the original or reopened job inventory was empty. This is a plausible
mechanism for the reported all-day exit failure, not proof of that user's trigger.

The first diagnostic change (`7f7915b`) preserves the 30-second budget, two quiet pairs, all job
statuses, pending-export checks and nonzero cleanup behavior. Unit matrices cover
retained jobs, partial/slow observations, API failure at deadline, capped count-only
diagnostics and unchanged client exit codes/no export after refusal. It adds no
automatic fallback, job deletion or special exemption for cancelled jobs.

Idle query timing and held-stdin native checks did not reproduce a transport or
stdin-induced timeout. Ordinary interrupted foreground/background jobs recovered
after their synthetic work completed. These observations and native regression
fixtures are offline/process evidence, not a live rootless-Podman pass, an all-day
soak or acceptance of a repair. Native job cleanup remains open; the subsequent
snapshot candidate below establishes a stronger teardown boundary rather than
assuming a reaped host-side Podman wrapper proves the remote service stopped.

#### Timeout-only wait timing diagnostics (preparation)

The diagnostic-only amendment adds a separate `History wait timing:` line on
`activity-timeout`; the existing `History wait timeout:` line must remain
byte-compatible, with no normal-success chatter. Prepare focused local coverage
for all validated complete pairs versus recovery-eligible pairs, per-kind query
counts/totals/maxima (including failed and timed-out attempts), the last attempt's
kind/outcome and remaining whole grace at its start, and the last complete pair's
age. The remaining-grace field is not the individual request cap. Check zero
budget as zero query counts with unavailable last-attempt/observation-age fields,
`9999+`/`9999+s` caps, and no fabricated pair from late or partial responses.
`returned` describes transport/API-helper return, not successful validation.

These are wall-clock timings around the existing native API helper, including
Podman/Python/native CLI startup and JSON decoding, not pure server latency.
Timing itself must retain the existing deadline, 30-second grace, polling interval,
60-second request cap, recovery eligibility and exit policy, without extra
probes or native job cleanup. Output must exclude IDs, paths, commands, payloads,
raw errors, environment values and credentials. Aggregate timing does not track
job identity, distinguish live from durable jobs or prove stuck/orphan jobs.

A reported Ctrl-D exit during an active turn retained a `foreground-active`
recovery refusal; a later successful standalone manual sync does not invalidate
that genuine active-work observation or authorize live export.
Ordinary manual sync remains separate from the guarded post-stop transport
fast path and is not optimized by this amendment. This preparation note records
no new test pass, replayed historical gate or external-host acceptance; earlier
evidence and its exact source/native identities remain unchanged.

#### One-attempt automatic archival (preparation)

The later routing amendment requests one bounded post-stop Reader attempt
instead of immediately asking for manual sync after an eligible owning-client
live failure. It never requests a second unrestricted manual-sync attempt if
the guarded reader refuses or fails. Keep the 30-second live grace, 60-second
request cap, 35-second transition and 180-second snapshot budgets unchanged.

Prepare focused coverage for the supported 2.3.x foreground-timeout route:
two consecutive full validated pairs must show nonempty foreground activity
and only shell/subagent jobs, including an empty job list. A later empty
foreground response resets this hint; malformed/unsupported responses and
known non-timeout API errors invalidate it. Actual query timeouts may retain
previous qualifying pairs without inventing observations. No hint authorizes
live export. Retain the 2.2.6/2.2.7 foreground refusal and unknown-version,
contract, image-job and incompatible-metadata refusals.

Positively classified transport and unsettled-history failures during live
wait/sync may route once only on the known 2.3.x profile with a captured safe
source. Unmarked `BoxError` validation failures do not enable a retry.
Explicit compatibility and
Reader refusals or uncertain source cleanup must not retry; `KeyError` and
`ValueError` must not become broad automatic-retry triggers. Require clean owner
closure, all clients detached, actual verified whole-container stop, the same
passive container and immutable identity, stop-intent/admission checks, guarded
Reader checks, native export/import verification and independently bounded
cleanup. No normal launch or runtime/config/auth/vault refresh, new resources,
job replay/completion or queued-input acknowledgement is permitted.

Check owning TUI/web outcomes separately from archival: foreground-interruption
recovery remains nonzero even with raw client `0` and complete saved history;
raw nonzero and unknown client results remain failures. Report archive success
only after verified cleanup/final stops, never as work completion. Pending
sessions must preserve their prior archives/checkpoints while settled neighbors
make verified progress. The `HistoryPending` aggregate must report the fixed
`snapshot-incomplete` category and progress/interruption warning, not all-history
success. Manual-sync guidance is for unsafe or unfinished automatic archival.

The native foreground fixture options (`--foreground` and
`--producer-close graceful|abrupt`) retain the original default regression and
are separate CI steps for the original 2.3.0 baseline and selected stable 2.3 patch.
Source-bound local native 2.3.2 checks passed the default, graceful-foreground and
abrupt-foreground cases. Both foreground cases preserved genuine queued input and
execution claims in source and destination, with no additional provider, shell
or plugin execution and one Reader lease. Graceful close archived all 13 source
sessions. Abrupt loss verified 12 while retaining the interrupted session's prior
archive/checkpoint and honestly reporting one pending session. Neither outcome
declares the interrupted work complete.

The earlier foreground fixture attempts remain failed setup evidence: a held
provider request or a flushed streaming delta does not guarantee a durable
unfinished assistant projection, and a nonempty native message page may carry
an empty-tail cursor. The corrected fixture requires actual foreground activity,
native queued admission and checked exports, rather than fabricating a pending
row or weakening history validation.

These synthetic local native checks do not establish whole-container process death
or external-host acceptance. Do not relabel earlier history/recovery reports or
infer a new host pass from them.

The owner separately reviewed three private focused host reports for exact source
`5135664da3ab6ba2aab8b7f60b5709176ec4448e` (`1.7.244.dev0+g5135664`),
including source, native binary and approved runner hashes:

| Host case | Checks | Observed outcome |
| --- | ---: | --- |
| Graceful | 31/31 | All 13 archives verified. |
| Stop-cancel | 29/29 | Explicit stop prevented both restart and Reader launch. |
| Live transport | 31/31 | Recovery verified all 13 archives without consuming queued work or changing execution markers. |

This evidence is scoped to the history-fix commit and those cases. It does not
establish a host pass for the combined clipboard/history release, other runtime
changes or interactive clipboard composition. The private reports are not
distributed with the release.

#### Post-stop persisted-history snapshot candidate

`history_recovery.py`, `history_reader.py` and `workspace_control.py` add a one-shot
snapshot path after eligible timeouts. The original profile is shell-only; the
native 2.3.0 retained-subagent extension is described below. The ordinary live wait still
fails. Eligibility is based on two prior complete validated pairs, not the final
query phase: a tiny remaining CLI-query budget must not randomly disable the
snapshot. A real timeout may retain those observations, but contradictory late
responses and known non-timeout API failures invalidate them. All versions/kinds
outside the supported version-specific profiles retain the existing pending behavior.

Unit/process matrices cover private stop intent before admission, stable locks,
uncertain transition fences, immutable resource identity, stop/start/snapshot/stop
ordering, one isolated reader, deadline propagation and unchanged nonzero results.
Malformed/unsafe controls never initialize or repair themselves from readers.
Source and destination scheduler guards inspect metadata only, use WAL-visible
read-only SQLite connections and refuse every schedule row/unknown schema. The
native scheduler's rewake path can run despite `-schedules`; no-schedules metadata
and system-configuration guards are therefore prerequisites, not optional hints.

`tests/smoke_history_recovery_v2.py` runs on unmodified 2.2.6, 2.2.7 and 2.3.0. It closes
the old **local native process**, then exercises the production reader, real
archive/import verification, retained running-looking job metadata, no invented
notices and source/destination scheduler refusals. Provider/shell/plugin canaries
must not execute during the reader. It is included in `tests/smoke_v2.py`; it does
not establish whole-container process death, actual rootless stop/start ordering
or host acceptance. The original live timeout and nonzero result remain distinct
from a successful checked snapshot; native job cleanup is still not repaired.

The development 2.3.0 backport retains stable-line commits `328f83e`/`7f441b8`'s
exact-version policy, source/destination guards and ownership-safe native fixture.
On 2.3.0, reload drains jobs: the fixture instead creates a real held running shell,
proves the ordinary live timeout, binds the owned shell before producer termination
with a pidfd, then stops/reaps only those controlled local processes. It verifies
complete durable job/schedule/message/part rows remain unchanged and refuses all
eight source and eight destination scheduler cases, including unknown/WAL rows.
`tests/test_native_recovery_process.py` covers ownership/identity refusal, no bare-PID
signal fallback, descriptor cleanup and prior subreaper/umask restoration.
These are focused local/native checks, not new external-host acceptance or a waiver
of the unfinished dashboard checks. The devel host runner below is not ported to
the current-native profile and still requires its recorded 2.2.6 version.

For a narrow recovery backport, run from the repository root:

```sh
PYTHONPATH=src:tests python3 -B -m unittest \
  test_history_reader test_history_recovery test_native_recovery_process
python3 -B tests/smoke_history_recovery_v2.py \
  --opencode /srv/native/opencode-2.3.0 --temp-dir /srv/private-tmp
```

Keep the tested source/working-tree identity stable for each run. Required
package/build-only publication validation remains separate; old accepted host
reports do not become passes for the new source merely because guards match.

Exit-routing regression coverage composes the real `launch_environment()`,
`launch_client()`, baseline capture and readiness waiter, substituting only their
external boundaries. A named matrix covers eligible recovery, custom-container
refusal, non-shell jobs, one completed observation, unsafe/uncertain control,
attached clients and failed/unverified owner-wrapper closure. Identical public
activity counts must produce distinct fixed refusal categories without attempting
live export after readiness failure, weakening recovery eligibility, changing
client failure results or starting recovery before owner closure. Separate tests
keep unknown diagnostic categories, exception contents and metadata out of output;
capture diagnostics never add native probes or repair control state.
This closes an observability/test-composition gap, not proof of the cause of an
uninspected host timeout or new whole-container acceptance.

#### Retained subagent jobs on native 2.3.0

The upstream `v2.3.0` implementation (`a6b15d33daf6ab7d35569f6d711fdb630b9f04e7`)
merges durable job rows with a process-local job registry. Its foreground-active
endpoint describes process-local execution, not historical transcripts. A new
unregistered process can therefore expose zero foreground sessions alongside
retained running subagent jobs. Ten jobs alone are not evidence of a slow active
query, and a timeout in the `foreground-query` phase does not establish that one
request consumed the whole budget.

The dedicated `smoke_history_subagent_recovery_v2.py` fixture creates real native
subagents through a bounded loopback provider, closes/reaps the producer, and
exercises the full production `Reader`, unchanged checked exports, host archive
imports and independent native verification. It observes one reader and no
additional provider, shell or plugin execution. Durable jobs, execution claims,
inbox and history must remain unchanged, with no fabricated completion notice.
Saved and project configuration canaries, source/destination schedule refusals,
and ambient credential exclusion remain part of the test boundary.

The original extension gave only exact native 2.3.0 shell/subagent snapshot eligibility after repeated
validated foreground-idle observations. The live wait still refuses all pending
jobs, and the original nonzero interruption result remains. Before native history
processing, the new profile validates durable job schemas and recovery metadata;
image/unknown kinds, unknown recovery forms and legacy background rows refuse.
The source remains unregistered, while destination clients remain standalone.
Managed restart continuity and ordinary workspace preparation are not fallbacks.
The original source/destination scheduler, ownership, stop-intent, uncertain-start,
cleanup and fresh-verification guards remain authoritative.

```sh
python3 -B tests/smoke_history_subagent_recovery_v2.py \
  --opencode /srv/native/opencode-2.3.0
```

These are synthetic local/native checks, not whole-container stop/restart or a
new external-host acceptance. The earlier 42-check history and 12/six recovery
reports retain their original source, harness and native identities; they do not
accept this new profile. Image-job recovery remains unaccepted. Do not infer the
kind or cause of a real user's retained jobs from a matching synthetic count.

##### Focused external-host evidence; formal gate remains incomplete

A later synthetic host run used clean source
`631d317b0b6d5c4bf42d1d95d2138dde97b2ab3a`, native 2.3.0 and Podman 6.1.4.
It recorded an eligible live timeout, verified stop/same-ID restart, one fenced
Reader, successful snapshot and twelve native-verified archives, retaining
launcher exits `[0,1]`. The original v3 runner nevertheless remains **FAILED,
20/29 checks**: its import observer expected `import PATH`, while the native
adapter emits `session import PATH`. The assertion also short-circuited its
subsequent digest comparison; nine final named checks were not completed.

A separate guarded informational audit, without native OpenCode or lifecycle
execution, then confirmed twelve current archive bindings and matching baseline
logical row hashes in all seven source/original-destination table scopes,
including ten retained source jobs and one original destination job. Both known
shell markers matched one execution and the current owned container was stopped.
Original plugin/schema/export/digest/output/process baselines were not all
serialized and remain unavailable. This supplements the historical observations;
it neither retroactively passes the original gate nor proves every no-execution
or final-cleanup assertion. Raw fixture evidence remains private.

These observations belong to the exact source above, not automatically to later
integrations. They support focused development integration, not the retained
42/12/six gate identities, stable promotion or security certification.

#### Stable native 2.3.x recovery compatibility

The later compatibility amendment replaces the exact 2.3.0 selector with one
shared profile for canonical stable `2.3.N` identities, including 2.3.1/2.3.2
and future patches. Version routing is not native execution acceptance: the
existing strict source/destination scheduler, job/recovery metadata, filesystem,
system-config and deadline refusals remain mandatory. Executable versions must
match the exact captured patch on both sides, not merely the selected series.
Other minor/major versions, prereleases, build suffixes, noncanonical leading
zeros and malformed/nonstring identities remain refused.
Known contract refusals must produce fixed-category upgrade guidance without
printing native errors or protected metadata. Unsupported versions warn before
the owning client; schema/metadata and malformed activity responses warn when
detected. Ordinary activity, timeouts, stop requests and cleanup failures are not
misclassified as version incompatibility. Unknown minor/major startup behavior
cannot be certified from a matching schema alone.

Exact-tag source inspection found that 2.3.1/2.3.2 replaced `schedule` with
`automation`, whose startup rewake still requires an empty-table guard despite
disabled feature flags. The runtime recognizes exactly one known scheduler
column layout; it refuses missing/ambiguous/changed columns and every row,
including terminal/unknown statuses and committed WAL rows. Native 2.3.0 is
refused on the newer automation layout. Optional subagent `outstanding` metadata
has bounded, explicit shell/subagent validation, with image/Code Mode, nested,
unknown and malformed forms refused. This inspection is not native execution or
host acceptance.

These are column-layout checks, not a complete SQLite-schema fingerprint:
defaults, triggers, indexes and foreign keys are not compared. Passing them
does not prove an unknown future binary's startup behavior remains non-executing.

Focused source coverage exercises capture, host/remote Reader admission, exact
binary matching, shell/subagent timeout routing and the real owner exit path on
2.3.2 and representative future-patch identities. Synthetic changed schemas and
unknown recovery forms must still refuse before native startup. The existing
native fixture now accepts a selected stable 2.3.x binary without relaxing its
real-job, non-execution, archive/import, source/destination preservation or
scheduler canaries; CI retains 2.3.0 and follows the latest stable 2.3 patch using
the `~2.3.0` npm range, recording the actual selected version and binary hash.
The current native target for this amendment is 2.3.2:

```bash
python3 -B tests/smoke_history_subagent_recovery_v2.py \
  --opencode /srv/native/opencode-2.3.2 --temp-dir /srv/private-tmp
```

Keep actual runs source/native/hash-qualified. A fixture accepting future
versions is not evidence that they have run or that native startup behavior
cannot change. This amendment creates no new external-host acceptance, does not
replay the historical failed/incomplete runner, and does not change the
30-second grace, cleanup/result policy or required manual-sync failure guidance.

The local compatibility amendment based on `cb1e299` passed 22 selected source
methods across its focused run and one corrected negative-fixture case. That
case initially left the automation schema unchanged because its replacement
literal assumed a space instead of a newline; only the fixture was corrected.
The first native attempt also failed and remains failed evidence: a leftover
fixture call selected the legacy scheduler guard instead of the tested version.
After passing that version explicitly, a fresh synthetic native **2.3.2** run
passed in 51.0 seconds, with unchanged tracked bytes during execution:

* Native executable SHA-256:
  `e6543f6e5c39c40971ba38afd1819ac852c29ac735b5a89065d3d148d47f27ad`.
* Ten source subagents, twelve native-verified sessions and one Reader lease;
  one retained destination subagent and both original destination sessions
  preserved, including real standalone archive replacement.
* Source and destination both used native `automation`; all eight refusal cases
  per side passed, including committed WAL cases.
* Producer exits were clean and unforced. Durable execution/history were
  unchanged, with no extra provider, shell or plugin execution.

The frozen tested runtime hashes are:

```text
d7c3ae933b74f0184bfd318f111c77959dc1177e882e9ad931d3b338a13987be  history_reader.py
13a85ccfd5b022148b2adab0808c51c7e34312d069e099e9a8310c7ddba64e92  history_recovery.py
a62a85b636f05ececf675d49220201f1a8e51de2893055859753de417d18f9e9  workspace_service.py
8a1201ee625fb1b9825846900ca68fa7db161469a4162bff36cb060ff804bf4f  oc_box.py
```

Nonempty `outstanding` forms have synthetic metadata coverage, not a separately
observed native reproduction. Native 2.3.0 remains in CI but was not rerun
locally for this amendment. No full local suite, host Podman fixture, retained
recovery replay or new external-host acceptance is implied by these results.

#### Focused shutdown-outcome policy checks

The approved outcome-only amendment preserved the existing 30-second oc-box
live-readiness grace and was originally scoped to exact-version recovery guards:
shell-only on native 2.2.6/2.2.7, shell/subagent on 2.3.0. The later series
compatibility amendment above broadens eligibility, not that grace or its
required metadata checks. That grace is not a native export/import
timeout. Automatic exit can spend it before checked archive copying; manual sync
omits the original owner's live wait. Both already use the same checked
`sync_sessions` implementation. Recovery must not recursively invoke the CLI,
perform ordinary preparation or refresh vault/configuration/plugins/credentials.

Limit this change's focused validation to a small local fake-boundary matrix,
with no native processes, Podman, host services or replayed acceptance fixtures:

| Case | Required policy/output assertion |
| --- | --- |
| Raw client `0`, eligible live timeout, fully successful recovery | Return `0` only after all checked exports/imports/native verification, recovery cleanup and final stop complete. Keep the explicit interrupted-execution/unconfirmed-background-completion warning; do not conclude that overall history sync failed. Exercise owning TUI and web routing. |
| Actual client failure or unknown status | Preserve raw failure `7` and cancellation even if archives were saved; unknown raw status stays nonzero. A recovered archive is not proof of client success. |
| Incomplete or failed recovery | Pending/partial history, failed export/import/verification, recovery failure, or failed/uncertain cleanup/stop remains nonzero and retains the existing retry-`oc-box sync` guidance. No replacement resource, ordinary preparation or extra credential refresh is allowed. |
| Progress and privacy | Distinguish the live wait from archive copying. Check only aggregate sessions/exported/unchanged/updated/pending counts, elapsed time and fixed `post-stop-reader`/`private-endpoint`/`standalone` modes; exclude IDs, titles, argv, environment values, native errors and model values. |

Eighteen selected local methods covering these routing/output contracts passed
on the working tree for this change; no native/host gauntlet was run. This is
local regression coverage, not new external-host acceptance. Source jobs,
execution claims and queued input remain
untouched; job age/status does not waive a guard, and no global autoaccept is
introduced. The summary does not speed source exports or archive writes; further
performance optimization is deferred.

Keep all earlier source identities, failed/incomplete reports and recorded
`[0,1]` exits unchanged. In particular, the `631d317` observations above and the
reviewed historical reports below are not acceptance of this later outcome
policy. A later release needs separately scoped, source-qualified formal
acceptance; do not silently reuse the old recovery exit oracle or relabel its
results. No new external-host, full UI, stable-release or security claim follows.

#### History-sync performance and integrity

Measure the whole pass, including private-service startup/shutdown, rather than
only timing requests to an already warm service. Separate first import, unchanged
history and one changed session. Use private synthetic source/destination stores,
include child sessions and long paginated transcripts, and record the exact
source/native identities. Never benchmark by rewriting a real user's archives.

The native 2.3.0 source-only comparison used an integrated working tree based at
`9f6e407`, with runtime/fixture hashes frozen and checked before/after each run.
It compared original manual per-request source startup against the enabled
quiet-source lease, including its preflight, startup, workspace lock and cleanup.
Container startup/configuration refresh and interactive rendering were excluded.

| Synthetic workload | Per-request source | Quiet source reuse | Time reduction |
| --- | ---: | ---: | ---: |
| Five sessions, first import | 8.253 s | 7.486 s | 9.3% |
| Five sessions, unchanged | 2.432 s | 1.514 s | 37.8% |
| Five sessions, one changed | 4.920 s | 3.766 s | 23.5% |
| 109 sessions, unchanged | 46.448 s | 19.052 s | 59.0% / 2.44× |

The five-session case included a child and 960 messages / 2.12 MB. The larger
case first established real verified checkpoints with 109 imports and 109 fresh
verification exports; neither warm arm fabricated state or skipped checked
exports. Both larger warm passes retained checkpoint bytes and performed zero
destination calls. Source native-service starts fell from 110 to one, plus one
version probe; source Python inventory/export bridges remained 110 in each arm.

These are single paired, quiet-eligible synthetic measurements, not statistical
or production-host guarantees. Known jobs/schedules and unsupported sources keep
the slower path. Existing-owner and post-stop-reader paths are unchanged.
Destination writer reuse was investigated but not enabled: its longer lifetime
was not established safe enough to replace the existing independent reads.

For the distinct stock 2.4.0/2.4.1 manual source lease, run
`python3 -B tests/smoke_history_source_v24.py --opencode /path/to/clean/native --release 2.4.1`
with each binary and its matching release argument. Compare ordinary standalone and reusable native source
startup counts and the bytes of their checked exports. Verify pre-start
schema and durable-work refusals, clean handoff/reaping, and that queued work
and execution claims are not consumed by the optimization. The fixture
isolates source work: it does not measure normal configuration preparation,
host destination writes, or rootless Podman.

The explicit stopped Reader route must hold the lifecycle lock, preserve the
workspace registration, refuse unknown resources before start, and never
replay ordinary preparation. Exercise stopped identity, pending history with
verified independent progress, post-read cleanup failure, and an ambiguous
start that retains its persistent uncertainty fence even after a later stop.

Keep the following boundaries when optimizing:

- Read every inventory page and perform full checked source exports. Timestamps
  are not transcript-integrity tokens, and unfinished sessions must retain their
  prior archives while independent sessions make verified progress.
- Keep destination mutations/checkpoints serial. Child-cascade guards and full
  host verification must remain independent fresh-native reads, not writer
  self-confirmation or cached child inventories.
- Avoid repeated source startup only with a private pass-local lease whose
  eligibility, configuration/store selectors, credential lifetime and cleanup
  are validated. Do not expose a shared or attachable maintenance service.
- Prefer the existing slower path when safe reuse is unavailable. Never replay
  an uncertain mutation or silently restart a failed optimized service.
- Retain recovery's per-command admission, deadlines and source/destination
  scheduler/system-configuration checks. Ordinary performance work is not a
  waiver of the verified-stop recovery boundary.
- Bound all helper lifetimes and private transport, reap owned children on every
  exit path, and print overall success only after successful cleanup.

Native tests must cover fresh independent readback, conflict/no-op detection,
pending-neighbor progress and unchanged archive/checkpoint data. Process counts
and elapsed times are useful measurements, not integrity or host-acceptance
proofs. Rootless lifecycle acceptance remains host-only.

##### Guarded-reader export transport reuse

The later performance amendment, based on `44b1c14`, targets the remaining
per-operation container/Python bridge startup in the post-stop Reader path.
It does not replace the Reader lease, remove native export/projection comparison,
trust timestamps, reuse destination writers, change the 30-second live grace or
optimize ordinary standalone/manual sync.

One completed fake-loopback component comparison used 136 exports of 3,072 bytes
and eight messages each. The old bootstrap/Reader/bridge path took 22.861 seconds;
one pass-local helper took 0.240 seconds, including its startup and clean exit.
Both made the same 272 message/export HTTP requests and received 814,368 response
bytes. These observations exclude Podman, native OpenCode, source preflight,
inventory and destination work; they do not predict a production-host speedup.
Earlier timed-out/incomplete measurement attempts remain excluded, not passes.
Archive-only work was small in that synthetic workload, so archive bytes,
rewrites and digest/checkpoint semantics are deliberately unchanged.

The new helper accepts only serial inventory/export requests against the existing
Reader endpoint. Host admission is checked before each bounded request write,
not only at process creation. Credentials use private stdin, not argv, logs or
saved config. Sequence/size framing, one-response memory bounds, deadlines,
pending-neighbor progress, no automatic restart and reaping/EOF checks are
mandatory. A clean process exit with trailing output is not clean framing.
Final success is withheld until helper cleanup succeeds.

Focused pipe/loopback tests cover the transport boundaries; existing V1 and
conflict/no-op verification regressions also passed. The existing native 2.3.2
retained-subagent fixture passed in its one-child mode in 16.4 seconds: three
sessions verified, one native Reader, eight source and eight destination
automation refusals, preserved durable execution/history and original destination
sessions, and no extra provider/shell/plugin execution. This is local synthetic
native coverage, not new external-host acceptance or a real-project timing.
No broad local suite, host-fixture replay or native workload sweep is implied.

#### Native terminal preferences and open tabs

Native terminal preferences and tab membership are not the history inventory.
`test_sync_config_preferences` checks opaque, container-local `cli.json`
preservation, first-use host seeding, unsafe file/path refusals and cache/copy
failure behavior. `test_native_tabs` checks bounded read-only native state and
selection; `test_native_tab_routing` composes that policy with actual launcher
selection and controller callbacks. Closing tabs must not remove exported
sessions, invent a conversation at home or reactivate archived roots.

Controller `info` refreshes hold a temporary metadata lease; only `attach`
retains it after the reply. Admission inventory uses the isolated providerless
client transport with an explicit existing endpoint, never owner credential
forwarding or an implicitly discovered source. Session-bound clients refuse
an unbound home state; location/catalog clients may retain their normal lease.

The provider-free native 2.3.0 PTY fixtures exercise literal rendered X-close,
final-tab close, quit/relaunch, explicit picker reopening, and real preference
changes followed by production config refresh. Native owns every tab/preference
write. Messages and source/host preservation are independently checked:

```sh
python3 -B tests/smoke_native_tabs_v2.py --opencode /srv/native/opencode-2.3.0
python3 -B tests/smoke_native_preferences_v2.py --opencode /srv/native/opencode-2.3.0
python3 -B tests/smoke_image_preview_preferences_v2.py --opencode /srv/native/opencode-2.3.0
```

The image/preference reconciliation fixture covers absent and explicitly false
base image fields. Real native settings changes must save unrelated preferences
without persisting the forced inline image booleans; UI cycles establish that
the overlay is active and that unflagged restart returns to the base. Frozen
pre-reset host bytes, first-use seeding, additional-client inheritance and
noninteractive-client exclusion also retain focused unit coverage. This does
not demonstrate actual terminal image rendering.

These are synthetic local native checks, not interactive external-host
acceptance, notification-delivery or permission-enforcement certification.
Native stores open membership/order, not the last focused tab. An initialized
empty native scope now remains at home; absent native state retains the legacy
selection/creation path. Existing archived-root/history reports retain their
original source identities and do not validate this new conditional UI policy.
Before stable promotion, use fresh synthetic state on the exact clean candidate
outside oc-box to observe production config refresh, X-close/final-close,
quit/relaunch/picker reopening and terminal/web/sharing/secondary-client leases.
Do not resume retained host fixtures or relabel prior unfinished UI gates.

The focused host runner adds opt-in `--history-recovery-case snapshot` and
`--history-recovery-case stop-cancel`. Each invocation creates a separate fresh
synthetic root. Their `oc-box-history-recovery-host-v2` reports require 12 and six
checks respectively, owner exits `[0,1]` in each, and explicit-stop exit `0` in the
cancellation case. They are **additional to**, not replacements for, the unchanged
default 42-check/eight-exit run. The new cases remain strictly native **2.2.6**;
2.2.7 reader results are not mislabeled as host acceptance.

Run outside oc-box as an unprivileged user, with the runner outside a clean,
full-history candidate checkout and an existing safe work parent:

```sh
python3 -I -B /srv/oc-box-harness/tools/test_session_sync_podman.py \
  --source /srv/oc-box-candidate --expect-commit "$CANDIDATE_COMMIT" \
  --work-dir /srv/oc-box-acceptance --opencode-v2 /srv/native/opencode-2.2.6 \
  --history-recovery-case snapshot
# Repeat the same command with: --history-recovery-case stop-cancel
```

The snapshot case must exercise the actual default 30-second production refusal,
old owner closure, full stop proof, exactly one pinned-ID restart/private reader,
real verified host archives and final stopped state. The cancellation case uses
independent control processes; the stop process is preloaded but cannot invoke
the CLI until the real timeout/owner closure, then publishes intent before held
admission is released. Missing reproduction is a failure, not a drained-job pass.
Only forwarding observers are permitted; selector support must preserve actual
production fences, never manufacture them. Reports retain fixed categories and
source/native/runner identities, never argv/env/transcripts.

These first two cases do not establish naturally ambiguous engine-start behavior
or every stop-timing race. Keep unit/process fence coverage and obtain the relevant
additional host fault/race and lifecycle/vault/history evidence before stable
publication. Never run these fixtures here, move retained stores, delete private
reports to make a checkout clean or overwrite earlier failed evidence.

The first recovery snapshot host attempt against `d736bc1` failed the exact
transition oracle after observing the eligible timeout and old-owner closure.
That report remains **failed**, not an accepted snapshot. An offline regression
identified a fixture defect: the v1 observer used any active recovery deadline to
identify phase, so the launcher's separately budgeted outer cleanup could become
an extra recovery inspection/stop proof. The old report does not retain those
events and cannot exclude an additional runtime failure.

The v2 fixture forwards the actual recovery function unchanged, scopes observation
to that call/thread, and records the real boolean completion result separately.
The seven required recovery transitions and two full-stop proofs remain strict;
unexpected operations inside recovery still refuse. A fresh run may test the same
clean `d736bc1` runtime with the corrected external harness and its own recorded
runner hash. Preserve the old candidate, runner, report and retained storage.

#### Reviewed focused recovery host acceptance

The following stable backport note describes the historical 1.6 scope only:

**Stable backport scope:** the bounded six-commit backport is based on stable
`c336b419d5760c771d869de436ca6c328af8cc73` (`v1.6.60`), retains version series
1.6 and the shared baseline 29, and does not import the 1.7 TUI/sharing or
archived-root automatic-resume features. Its default harness retains the stable
33 checks, stages `graph`, `readiness`, `background`, `shell`, `recovery-sync`,
and ordered exits `[0,0,0,1,0]`. Reports require every check, complete stage and
those exact exits; the 12/six recovery cases remain separate. The historical
reports below are retained verbatim with their original source, runner and scope.
There is **no new external-host acceptance** for the stable backport or any later
channel-integrated candidate: neither local unit/native checks nor matching core
runtime hashes relabel the original 42+18 pass. Freeze the integrated candidate
and repeat relevant external-host history/recovery and lifecycle/vault gates
before stable publication. Never run the live Podman fixtures inside oc-box.

Two operator-provided v2 reports passed review against clean runtime
`d736bc1bb33ffeedee65ad3ec281de77c1e31847`, count **115**, version
`1.7.86+gd736bc1`. All **44** runtime/donor hashes match that Git tree. Both pin
native **2.2.6**, binary SHA-256
`50503d24577fa4117a7c95ee85b2cd679606a87eacf20ca54ed5db5c9e1cf895`, matching the
retained offline executable. The external runner is from `60df669`, SHA-256
`56668d8480f08e06a630bad5f6c214793f999e59a910496a7f22c8bbfb356d20`.

| Case | Reviewed results |
| --- | --- |
| Snapshot | 12 checks true; 34 observed cancelled-job pairs; one restart, one reader, one recovery call and completed snapshot. |
| Stop cancellation | Six checks true; 34 pairs; no restart/reader/snapshot; one recovery call and explicit-stop exit `0`. |

Both reports have owner exits `[0,1]`, zero preparation/authentication calls during
recovery, exactly two synthetic provider requests and one shell start, no primary
or secondary failures, and empty failure traces. Source manifest canonical-JSON
SHA-256 is `3dd3bd86e4d5dc5b6ae3baaa13151acd2222f1021cf6a2a01d42707637db4568`.
Report-byte SHA-256 values are:

- Snapshot: `192cc00c8931f6e087a0eef8459ebd0946b8b4368c7c587336d8b554c92c062a`.
- Stop cancellation: `bf87052f4917263779b250f7e0bef483263fd229d3f359e0b050a269810f524e`.

This establishes **18 focused synthetic, scripted external-host recovery checks**
on native 2.2.6, not completion of the interrupted work or repair of native job
state. The original nonzero result remains intentional. The earlier v1 oracle
failure remains failed evidence. No new baseline 42-check, lifecycle/vault,
interactive UI, real-provider, 2.2.7 host, exhaustive fault/race or security claim
is established by these reports. Keep raw reports private and the two fresh stores
and old failed store intact; later documentation/harness commits do not change the
recorded tested runtime identity.

#### Reviewed baseline history/readiness host regression

A separate `oc-box-session-sync-host-v1` report now passes review for the same
clean `d736bc1bb33ffeedee65ad3ec281de77c1e31847` runtime, count 115 / version
`1.7.86+gd736bc1`, the same 44-entry source manifest, the same external `60df669`
runner and the same native 2.2.6 executable identified above. All **42** named
baseline checks are true, with ordered owner exits `[0,0,0,1,0,0,0,0]`, complete
stage, no primary/secondary failures and an empty failure trace. Report-byte
SHA-256 is `9c94916f9e257e79c2a131df5fb25eecc50579d6358e834c36e99f0cd1749619`.
Its baseline v1 format is distinct from the earlier failed recovery-v1 format.

Together with the two recovery reports, the reviewed evidence establishes **60
scripted synthetic external-host checks**: 42 baseline plus 12 snapshot and six
stop-cancellation checks. The baseline adds history/readiness, child/archive
preservation, pending-session isolation, lease handling, background notice wakeup
and ordinary owning-start archived-root resume coverage. It does not repair the
retained native job, make the intentional nonzero exits successful or change the
earlier failed recovery report. Keep all raw evidence private and retained.

This is not a new lifecycle/vault/backup, interactive UI, real-provider,
network-containment, exhaustive fault/race or native 2.2.7 host pass. Stable
publication remains separately gated. The fix is integrated into
`feature/release-1.7` for the combined 1.7 rollout; this approval does not complete
final-candidate validation. Later documentation/harness commits do not extend the
recorded tested source identity.

#### History and interrupted-export contracts

The changes introduced in `d638907be97f9f33d00e59da526eb046e39a3627` add paginated
root/child discovery, independent host archives with relationship provenance,
missing-source reconciliation, verified native imports, and refusal of incomplete
or unsettled exports. Unit tests and `tests/smoke_session_sync_v2.py` exercise
these contracts using synthetic private databases and substituted transport. The
fixture also runs as part of the ordinary native V2 smoke suite.

Unfinished assistants that a session has already moved past are archived as
native's export, with a count-only annotation, while tail-unfinished assistants
and running shells/compactions stay refused. The superseded-assistant exception
also refuses active sessions. Pending sessions do not block the others, and
refusals show a fixed whitelisted reason. Unit tests
cover the classification, whitelist and isolation. `tests/smoke_v2.py` reproduces
the native state with real OpenCode 1.19.5 and 2.2.6: a V1 transcript containing a
never-completed assistant is imported by V1, migrated by V2, and production
`checked_export` and `archive_session` handle it. It also uses
`tests/interrupted_v1_fixture.py` to generate two actual SIGKILL-interrupted V1
turns with completed turns after them, plus a separate unfinished tail. Before
killing each owned native process group, a supported V1 export must confirm the
unfinished assistant has persisted. V2 migration must keep the observed shape;
only the superseded assistants may be omitted. Both imported and crash-generated
annotated archives must survive the native destination importer and production
re-export verifier. Migration leaves V1 bytes intact, and reopening V1 must retain
the exact crash-generated messages while leaving the inactive V2 bytes unchanged.
`tests/smoke_session_sync_v2.py` holds a real direct user shell while invoking
the production bridge subprocess and sync loop. Independent sessions before and
after that pending session must advance in the same refused pass, the pending
session's prior private/native archives and checkpoint must remain unchanged,
and only the fixed count/reason may be reported. Completion permits recovery.
An isolated-copy mutation that stops iteration at the pending session must fail
the later-session checkpoint assertion; it does not edit the checkout runtime.
Natively, 2.x settles an older unfinished assistant at the next turn; a killed
2.x server leaves a running
compaction until then, so those cases stay refused.

These checks are not live rootless-Podman acceptance. The accepted interactive,
vault-edge, and shared home-component reports below ran different source revisions
without this change; their scopes and identities remain unchanged. In particular,
`1.6.44+ga043049` and `1.6.44+gd638907` share a patch count, not the same source.
Repeat relevant host lifecycle checks and add focused history/readiness acceptance
on the clean integrated release candidate before stable GitHub/PyPI publication.
Status: the focused history/readiness runner, the global-vault fixture and the
V1/V2 lifecycle fixture have all since passed on clean `1.6.51+gb5d1ea8` (see the
reviewed reports below), and that exact commit was published as stable v1.6.51.
Mainline source updates and build-only distribution artifacts do not establish
that acceptance or trigger a stable release by themselves.

### Focused history/readiness host runner

`tools/test_session_sync_podman.py` is a separate host harness, not an installed
oc-box command. Run it outside its explicit clean `--source` checkout so the
candidate can remain unchanged while the harness is developed or updated:

```bash
python3 -I -B /path/to/harness/tools/test_session_sync_podman.py \
  --source /path/to/clean/candidate \
  --expect-commit FULL_REVIEWED_CANDIDATE_COMMIT \
  --work-dir /path/to/fresh-fixtures \
  --opencode-v2 /path/to/opencode-2.2.6
```

Supply the reviewed full 40-character candidate hash, not a version number or an
automatically selected moving branch. Host/root/container guards precede source,
placement and native engine probing. The selected source must have full history,
be clean and match that identity; imported runtime/helper paths and byte hashes
must remain bound to it, and every loaded module under that checkout must be a
hashed tracked file. On-disk bytecode caches are never read; ignored files other
than those caches and editable-build metadata refuse. The work/temp parents and
native executable may not sit inside the source. Runner and native executable
hashes are recorded separately. Never edit the candidate or change native
executables during a run.

The runner defaults to exact native `2.2.6`. `--native-version 2.3.0`
selects the separate exact `2.3.0` profile and must be paired with that
executable. The focused recovery cases require observed `cancelled` work
on `2.2.6`, but an actually held `running` job through the ordinary timeout
and native-client shutdown on `2.3.0`. These profiles do not establish an
external-host pass, and the same 42 baseline checks remain mandatory.

Use a regular unprivileged host terminal, outside containers and shell sandboxes.
Only fresh synthetic projects, config/history and a verified private rootless
Podman store may be used. The existing global-vault fixture's selectors, boot,
store, ownership and mount checks remain authoritative; a history-purpose marker
and source-workspace allowlist add restrictions rather than bypassing them. Do
not reuse a completed global-vault/lifecycle root for these cases.

The private-store proof accepts a missing `store.configFile` only for the
audited exact Podman `6.1.4` and `6.2.0` info schemas. It still checks the private
selector/config bytes, rootless local engine, overlay, graphroot, runroot,
volume path and image staging path. If a config path is reported it must match
exactly, even on those versions. This schema check is not host acceptance; an
unknown omission still refuses with `store-info-shape`.

Required history cases cover actual paginated root/child/grandchild/fork/archived
discovery and directory scope, independent archive provenance, parent-only
replacement, missing-source retention and genuine refusal of untracked native
host children. A held direct user shell must demonstrate native filtered export,
checked-export refusal, pending/nonzero owner exit with normal stopped cleanup,
then recovery after actual completion. Inventory updates and earlier successful
session exports are not an all-state transaction and need not roll back.

Required readiness cases distinguish pre-publication refusal, attached-client
lease drain, foreground activity and idle persistent PTYs. A foreground-active
child must keep one production waiter pending across several observed active
pairs before its provider response is released; a short deadline is not accepted
as proof. Refusals are pinned to their actual cause: host-child replacement to
the linked-child guard and the pending shell exit to that session's export. A
real synthetic provider-issued background shell must remain pending while
foreground is idle; its actual completion notification must then reawaken
foreground execution after the job disappears. One production history waiter
spans that handoff, observes both held phases without changing native
responses, and completes only after the final provider response is released.
The transient completed-but-unconsumed job row is not assumed to be observable
or claimed merely from an empty query.

Only scripted TUI-exit/observation callbacks and transparent forwarding spies are
permitted. Production launcher, Podman/native-CLI transport, authentication,
history exports/imports, permissions and errors stay real; gates release work,
never fabricate activity, success or refusal. Control credentials and private
native output stay out of reports. Retain reports/resources on failure and stop;
never move stores, rewrite markers, replay completed evidence or prune globally.
The only automatic removal is the private temporary staging directory created
under an explicit `--temp-dir`; fixture roots, reports and Podman resources are
retained. Image pulls use Podman's default registry-authentication lookup because
the real runtime directory remains available to rootless Podman; no registry
credential is read by the runner or recorded in its report.

Failed runs report their stage, fixed category and, as a bounded `failure_trace`,
only exception class names with `file:function:line` code locations (also printed
on the console); never messages, arguments, paths or native output.

Offline guard/oracle and native synthetic-provider checks are preparation only.
Record a host PASS only after reviewing the actual source-bound report and every
required check. This fixture does not establish interactive TUI, real-provider,
vault, backup/restore or network-containment acceptance.

**First host attempt (not acceptance):** runner SHA-256 `1b5ce6b2…f3d314` on
clean `1.6.51+gb5d1ea8` with native 2.2.6 stopped in the `graph` stage with
`operation-failed` after `paginated_inventory` (launcher exit codes empty). All
eight scoped sessions had already synced to eight host archives. The runner's own
archive oracle indexed `info["title"]`, which native 2.2.6 omits for the untitled
launcher-created session; production correctly archives it as `Session`. This was
a runner defect, not a runtime failure, and retains its evidence as a failed
attempt. An offline rehearsal against real native 2.2.6 with substituted local
Podman/transport layers reproduced it and passed all 27 checks after the fix; that
rehearsal is not host acceptance.

### Next migrated-history release candidate

**Historical handoff, reusable for a separately approved new runtime/stable
candidate—not instructions to resume retained fixtures.** Published `v1.6.51` remains
unchanged and its accepted reports predate the superseded-assistant correction.
The later [v1.6.58 release record](#published-v1658-evidence-reconciliation)
reports all three host fixtures, including that correction, on its exact source.
That reconciliation does not accept a later TUI revision. Freeze a clean new
candidate containing runtime, regression fixtures and final documentation, then
run all three fixtures on that exact revision before the first stable TUI
GitHub/PyPI publication; add the manager-specific observations above.

For the migrated-history baseline, the focused V2 runner retained its original
27 checks and added six pending-shell checks: all **33** were required, including
same-pass progress on both sides of a
pending session, exact fixed reasons and unchanged pending archives/checkpoint.
The current Textual candidate additionally requires the nine archive/resume
checks above (**42 total**); keep older 33-check report evidence scoped to its
original source rather than relabelling it as a new candidate pass.
The V1/V2 lifecycle fixture additionally records nine `superseded_history.checks`
for a synthetic interrupted V1 transcript through native import and migration,
the native omission/count annotation and destination round-trip, and preserved
V1 history. Require `superseded_history.included: true` and all nine checks on a
fresh full run; resume/retry does not establish them. This imported fixture is not
a host SIGKILL reproduction. Actual crash-origin and unfinished-tail regression
coverage remains direct native/offline; no new host tail test is claimed.
Do not infer a live active-superseded exception test from ordinary foreground
readiness checks: native V2 can settle older assistants when another turn starts.

A native preparation rehearsal caught an oracle defect before host handoff: the
new lifecycle check assumed a terminal first projection page. Native 2.2.6 can
return the four messages with a continuation cursor, then an empty terminal
page. The check now consumes validated pagination, rejecting duplicate messages,
invalid/repeated cursors and an unbounded page chain. The failed rehearsal remains
failed evidence; neither its diagnosis nor a corrected offline run is host acceptance.

Use the native executable setup in the
[V1/V2 host guide](opencode-2.md#automated-disposable-host-fixture).
From a normal unprivileged **host** terminal, substitute real paths and the full
reviewed candidate hash below. Both checkouts need full Git history. The harness,
both native executables and the fixture/temp parents must be outside the
candidate. Existing fixture/temp parents need enough disk-backed space for fresh
isolated image stores, encrypted full/portable backups and restores. Do not
select a retained fixture or any of its descendants as a parent.

```bash
(
  set -eu
  candidate=/path/to/clean/candidate
  harness=/path/to/separate/harness
  commit=FULL_REVIEWED_CANDIDATE_COMMIT
  v1=/path/to/opencode-1.19.5
  v2=/path/to/opencode-2.2.6
  fixtures=/path/to/fresh-fixtures
  temporary=/path/to/private-temp
  cd "$candidate"
  test "$(git rev-parse HEAD)" = "$commit"
  test -z "$(git status --porcelain --untracked-files=all)"

  uv run --isolated --no-project --with cryptography python -I -B \
    "$harness/tools/test_session_sync_podman.py" \
    --source "$candidate" --expect-commit "$commit" \
    --work-dir "$fixtures" --temp-dir "$temporary" --opencode-v2 "$v2"
  test "$(git rev-parse HEAD)" = "$commit"
  test -z "$(git status --porcelain --untracked-files=all)"

  uv run --isolated --no-project --with cryptography python -I -B \
    tools/test_global_vault_podman.py --opencode-v2 "$v2" \
    --distro tumbleweed --work-dir "$fixtures" --temp-dir "$temporary"
  test "$(git rev-parse HEAD)" = "$commit"
  test -z "$(git status --porcelain --untracked-files=all)"

  uv run --isolated --no-project --with cryptography python -I -B \
    tools/test_v2_podman.py --opencode-v1 "$v1" --opencode-v2 "$v2" \
    --distro tumbleweed --work-dir "$fixtures" --temp-dir "$temporary"
  test "$(git rev-parse HEAD)" = "$commit"
  test -z "$(git status --porcelain --untracked-files=all)"
)
```

Stop on the first failure and retain its original reports/resources. A retry or
new fixture is separate evidence; do not overwrite encrypted archives, move
stores or rewrite boot/identity markers. Review each printed report, exact
source/native/harness identities, every required check, deliberate refusal
exits, recovery and secondary failures. Share only the fixed-schema reports and
clean checkout identity, not raw native logs, environment or transcripts.

For a future separately approved publication, follow the
[publication checklist](../CONTRIBUTING.md#release-gate-and-publication-checklist):
select the appropriate stable/devel branch and version, preserve the validated
commit identity, and compare the exact public artifact hashes.
No new host pass or release is recorded by preparing this handoff.

### Published v1.6.58 evidence reconciliation

The public [v1.6.58 release notes](https://github.com/groopcd/oc-box/releases/tag/v1.6.58),
published **2026-11-09**, record stable source
`3138a8727c3638983a8b9d19b1cb93a83d94a027`, absolute commit count **87**, version
**1.6.58+g3138a87**. The entries below reconcile this document's older pending
migrated-history handoff with that publication. They are **reported by the
published release**, not inferred from a tag existing, and the raw external
reports were **not re-reviewed in this documentation session**.

| Scope | Recorded by the published release |
| --- | --- |
| Focused history/readiness | Reviewed external report: all 33 checks true, no failures. |
| Global vault | Reviewed external report: all 13 stages passed. |
| V1/V2 lifecycle and migrated history | Reviewed external report: six stages, nine superseded-history checks and 18 synthetic provider requests. |
| Exact-source automated gates | Source CI, seven distro images and the build-only package gate passed. |

These are source-bound scripted synthetic host scopes. They do not establish
Textual navigation/settings, embedded dashboard rendering/input, new journal/log
behavior, real-provider inference, real-project restore, network containment or
security certification. They do not make an older native OpenCode conversation
TUI observation into a Textual-manager pass. The current TUI candidate still
needs fresh external-host acceptance on its final clean commit; no new candidate
CI success, host run or publication is claimed here.

Keep the earlier reviewed reports below, runner/oracle failures, exact source
identities and all retained evidence unchanged. A later successful release record
does not convert a failed attempt into a pass.

### Reviewed host reports — 2026-11-09

These are user-supplied reports reviewed here, **not executions from this
container**, and do not replace earlier 1.5 evidence:

- Global-vault delivery, Build tools/permissions and full encrypted restore were
  accepted on clean **1.6.42+ge66fb39**, with native OpenCode **2.2.6**.
- Separate selected-vault V1/V2 full/portable restore, reset, downgrade and V2
  return were accepted on reported **1.6.43+ge06896f**, with native OpenCode
  **1.19.5 / 2.2.6** and **18 synthetic provider requests**. That source revision
  differs from 1.6.42 only in `AGENTS.md`; keep the execution versions distinct.

- A separate **operator-observed interactive** run on clean
  **1.6.44+ga043049** passed native Build tool execution without permission or
  credential prompts, TUI resize, simultaneous browser/TUI/persistent-shell use,
  shell-state retention across detach/reattach, independent web closure and owner
  waiting for the final shell lease before normal sync/shutdown. Four reports
  (owner, two terminal attachments and web) returned exit 0 without secondary
  errors; the synthetic provider recorded **9 requests, 0 errors** and all three
  tool markers. The helper requires native OpenCode 2.2.6.

- The separate focused history/readiness runner (`tools/test_session_sync_podman.py`)
  passed on clean **1.6.51+gb5d1ea8** (full commit
  `b5d1ea87729e3f681f530c38945c119133c55b2a`, count 80) with native OpenCode
  **2.2.6** (binary SHA-256 `50503d24…895`) and runner SHA-256 `ab26dffb…0528`.
  The reviewed report has status `PASS`, stage `complete`, all **27** named checks
  true, no primary/secondary categories, an empty `failure_trace`, `dirty: false`
  and launcher exit codes `[0, 0, 0, 1, 0]`; the single `1` is the deliberate
  pending-shell owner exit, followed by a successful recovery `sync`. The
  candidate checkout was also reported at the same HEAD with empty status
  afterwards. It covers paginated root/child/archived/fork scope, independent
  archives, parent replacement, missing-source retention, genuine host-child
  refusal/recovery, readiness drains, a real background-job wakeup and pending
  shell recovery with only scripted native API/CLI and TUI-exit callbacks.
  A first attempt with runner `1b5ce6b2…f3d314` failed earlier as described in the
  runner section above; it remains failed evidence and was not rerun or relabeled.

- The V2 global-vault fixture (`tools/test_global_vault_podman.py`) passed on clean
  **1.6.51+gb5d1ea8** (commit `b5d1ea87729e3f681f530c38945c119133c55b2a`, count 80,
  `dirty: false`) with native OpenCode **2.2.6**. The reviewed report's status is
  `passed` and lists all 13 stages from private-store/native-version through
  restored-latest-host-vault-history-os: first global-vault import and Build asks,
  running-owner rotation/reload/attach, mandatory denials and Plan, new-owner
  rotated/added entries, `--no-vault`, and full encrypted backup/restore with
  stopped byte-exact state.
- The V1/V2 lifecycle fixture (`tools/test_v2_podman.py`) passed all six stages on
  reported **1.6.51+gb5d1ea8** with native OpenCode **1.19.5 / 2.2.6** and **18
  synthetic provider requests**: V1 seeded lifecycle, selected-vault
  delivery/rotation/attach, full and portable V2 restores with fresh IDs and
  byte-exact stopped state, wrong-password reset preserving resources, and V1
  downgrade with V2 return preserving both histories. Its report records the
  version string and native versions, not a dirty flag or full commit; it ran
  from the clean candidate checkout.

The first two runs and the three 1.6.51 fixtures use scripted TUI/prompt input;
the 1.6.51 focused, global-vault and lifecycle runs ran the same published source,
and a later runtime change needs them repeated. The interactive run combines reports
with the operator's explicit stepwise confirmations: its unchanged
`NOT_AUTOMATICALLY_PASSED` field correctly avoids self-certifying observations.
It uses `--no-vault` and production attach-only client helpers, not ordinary
owner-capable secondary CLI routing. It does not cover interactive vault input,
Plan/deny conversations, reload permission prompts, a second full TUI attachment,
formatter/LSP/devices, real-provider inference or security certification.

### Fresh provider-free vault edges (host only)

`tools/test_global_vault_edges_podman.py` is a separate fresh-only fixture:

```bash
python3 -B tools/test_global_vault_edges_podman.py \
  --work-dir /path/to/fixtures --opencode-v2 /path/to/opencode-2.2.6
```

Require a clean unchanged checkout, unprivileged host and existing disk-backed
parent. Host Python needs the optional `cryptography` dependency; missing-crypto
children use a separate private stdlib-only virtualenv. Optional
`--temp-dir /path/to/staging-parent` also rejects retained roots
and descendants. No resume/replay selector or automatic cleanup. Keep its
`edges.json`, immutable per-case evidence and `edges-result.json` in place; it
never overwrites the original fixture's `result.json` or reuses completed runs.

Cases use real owning starts, scripted TUI callbacks/masked controlling-terminal
input and the production native session shell: populated delivery, removal while
the live owner's snapshot stays unchanged, empty and absent-vault fallback, then
actual start/reset refusal for wrong passphrase, corrupt vault and genuinely
missing optional crypto. Only synthetic private projects/config/history/values
are used. **Provider-free, no inference**; upstream catalog/package metadata
traffic may occur, with no network-containment guarantee. Optional Lemonade
discovery selects a fixture-owned empty-catalog stub, not the normal host service.
Production environment filtering and credential selection stay unchanged.

Offline `test_global_vault_edges_host_fixture` checks guards and bounded process
mechanics, not host acceptance. A separate reviewed user-supplied summary and all
ten case reports now **PASS on clean 1.6.46+gacddbd8**, count 75, with native
OpenCode 2.2.6. All required checks are true, with no primary/secondary failures.
The four owning cases return 0; the six deliberate start/reset refusals return 1
as expected. The fixture catalog recorded 0 requests and 0 unexpected requests,
not an audit of other network traffic. Earlier 1.6.42/43/44 scopes remain unchanged;
this adds no interactive UI, backup, real OAuth refresh, formatter/LSP/device or
real-provider claim. State preservation covers stopped resources and private
project/registry/home/native DB/config/auth bytes, not universal engine metadata
immutability: backend info/locks may touch runtime state.
Only native 2.2.6's exact `host-data-2/opencode/log/opencode.log` digest is
diagnostic rather than protected: `--version` appends to it before unlock.
Per-case before/after hashes are retained, and a read-only scan still refuses
known synthetic vault values/passphrases in that file. Other native files and
the entire container-home manifest remain protected. Removal checks require the
same verified native PID across the ciphertext change. Catalog verdicts use one
post-teardown counter snapshot; this does not prove daemon handlers drained.

This accepted run covers dynamic entry removal, initialized-empty and absent
vaults, and wrong-passphrase/corrupt/missing-crypto start/reset preservation.
It does not establish every policy-override transition or structured OAuth copy/
refresh case. Keep expanded acceptance tied to new evidence rather than relabeling
the earlier lifecycle or interactive runs.

### Fresh manual UI observation (host only)

`tools/test_interactive_podman.py` is a bounded **interactive-only** fixture, not
another full vault/lifecycle acceptance run. Use an unprivileged host terminal,
a clean full-history source checkout, explicit unmodified OpenCode 2.2.6 (the
default profile), and an existing disk-backed work parent. Never run it inside
oc-box or reuse completed fixture roots. No real project, operator credentials
or vault unlock is needed: the owning start explicitly uses `--no-vault` and a
synthetic `_API_KEY`. To run the separate exact 2.3.0 profile, supply its native
executable and pass `--native-version 2.3.0` to both owner and secondary clients.

```bash
python3 -B tools/test_interactive_podman.py start \
  --work-dir /path/to/fixtures --opencode-v2 /path/to/opencode-2.2.6
```

Optional `--temp-dir /path/to/staging-parent` changes only private temporary
placement. Both work and staging parents must be outside retained global-vault,
V1/V2 and backup fixture roots and their descendants, including renamed roots
with fixture evidence markers. No fixture files/store are moved or removed.
Choose a **short work-parent path** for interactive TTY tests. Secondary clients
use the fixture root's `temporary/` directory, even when the owner has a separate
`--temp-dir`. Podman/conmon creates a filesystem Unix socket there; its complete
pathname must fit Linux's 107-byte pathname budget (excluding the terminating
NUL). A deeply nested versioned work parent can allow pipe-based API checks but
fail TTY exec with a conmon socket-path error. Merely changing the owner's
`--temp-dir` does not shorten that secondary-client path. Leave any failed
fixture/evidence in place and create a fresh fixture under a shorter parent;
never move its graphroot/runroot, rewrite selectors or relabel the failed run.
The foreground owner prints its fresh exact root and secondary commands;
wait until its TUI is ready, then use those commands in other host
terminals. Only the single synthetic `source-a` workspace is allowed:

```bash
python3 -B tools/test_interactive_podman.py tui --fixture /path/to/printed-root
uv run --isolated --no-project --with 'textual>=8.2.8,<9' --with 'pyte>=0.8.2,<0.9' \
  python -I -B tools/test_interactive_podman.py manager --fixture /path/to/printed-root
python3 -B tools/test_interactive_podman.py terminal --fixture /path/to/printed-root
python3 -B tools/test_interactive_podman.py web --fixture /path/to/printed-root
python3 -B tools/test_interactive_podman.py stop --fixture /path/to/printed-root
```

The `manager` mode opens the actual Textual dashboard against this synthetic
fixture's registry/native host data, not the operator's ordinary history. It
does not acquire an outer client lease merely to monitor status. Explicit
workspace Connect and terminal actions acquire their own leases; record their
release and owner-wait behavior as part of the manual checklist above. Optional
Textual/pyte dependencies must be available to this interpreter. The `tui` mode
still means the native OpenCode conversation TUI, not the Textual manager.

Secondary commands require the unchanged source, original boot, verified private
store/resources and a live authenticated owning service; none starts a new
owner, refreshes credentials or accepts `--no-vault`. They dispatch directly to
the production authenticated client helpers, never the owner-capable launcher;
a lost/stopped service fails the client without ownership fallback. Do not
pull, edit or switch the source checkout while the fixture is active: successful
owner/client returns recheck the initial source identity after lease drain and
refuse a changed/dirty checkout without relabeling the recorded execution.
TTYs are required for UI paths. Web always uses `--no-open`: manually open its
private one-use pairing URL
without sharing or recording it. The provider stays alive in the owning
foreground process while its launcher and attached leases remain active.

Use **Build**, with one or two short synthetic prompts only (12 provider requests
per owner window). The unchanged deterministic provider exercises shell, MCP and
the permission tool; it does not support Plan/deny conversations. Manually record
TUI rendering/input, concurrent web/TUI/shell use, Ctrl-] shell detach/reattach and
history/shell-state continuity. Close clients before exiting the owner; `stop`
is an explicit verified-source stop, not cleanup, reset or resume. A later owning
observation requires another fresh fixture. Keep retained files/store in place.

Private `interactive-*-report.json` files record source identity, actual
launcher/client outcomes and owner provider counters/markers, never synthetic values or control
credentials. They explicitly leave manual observations **NOT automatically
passed** and never write the global fixture's `result.json`. Vault rotation,
interactive permission prompts, formatter/LSP/device checks and other lifecycle
acceptance remain separate. Offline `test_interactive_host_fixture` checks these
guards, lease-free manager dispatch and native relay dispatch, not real
Podman/UI acceptance.

### Shared home-component ownership (fresh host only, image-free)

`tools/test_home_ownership_podman.py` is a separate **component-only** probe:

```bash
python3 -B tools/test_home_ownership_podman.py --work-dir /path/to/fixtures
# After reviewing the new evidence, separately:
python3 -B tools/test_home_ownership_podman.py --cleanup /path/to/exact-printed-root
```

Use a clean full-history checkout, unprivileged Linux host, installed Podman/age/
age-keygen and existing disk-backed parent. Both work and optional `--temp-dir`
parents reject retained roots/descendants before source probes or allocation,
including renamed home-component roots retaining only purpose/journal/report
markers. Never run on a container or reuse/move an old store.
There are no images, containers, networks, OpenCode, credentials or workspaces:
only two explicitly labelled local-driver volumes in a verified private store.
The probe measures the real `podman unshare` UID/GID maps for IDs 0 and 1, checks
fixed synthetic directories/config/closed SQLite data and a mapped-1 sentinel
independently, then uses production private `archive_command`, `encrypt_stream`
and bounded owner-restoring `decrypt_stream`. Keys stay in memory/memfd; only
`home.age`, nonsecret fingerprints, purpose/journals and immutable reports persist.
Both root directories must be 0:0; extraction keeps its root private (0700),
not arbitrary archived root metadata. Atime, ACLs/xattrs and SELinux are not tested.

Both backup kinds capture `home.age` before the full-image branch in `backups.py`
and restore it before the image/container branches. This probe adds **only shared
home-component UID/GID/mode/content transport evidence**, not another whole full/
portable lifecycle, first-mount or keep-id/auto/custom-mapping acceptance. The
reviewed 1.6.42/43/44/46 scopes above and all completed roots remain unchanged.
Offline `test_home_ownership_host_fixture` (including real age through a fake
unshare dispatcher) is not host or mapping acceptance. A separate reviewed
user-supplied host report now passed on clean **1.6.48+g7df5074**, commit
`7df5074f4f8b8e9844c149cbad99cf6f1e93dcb3`, count 77. All 30 audited helper/runtime
hashes match that commit; all six proof checks are true, source/destination
manifest hashes match and no primary/secondary failures are reported. This
accepts only measured rootless 0/1 UID/GID, mode/content transport, source
preservation and root privacy through the shared production stream. It does not
relabel the earlier per-kind lifecycle evidence or establish container-origin,
first-mount, custom mappings or a security certification.

Cleanup holds the fixture lock, verifies purpose, original-boot marker, three
environment store selection and **both** exact volume metadata/paths before any
unforced removal. Missing/foreign/unrecorded/ambiguous state refuses. It retains
all files/ciphertext/images; never use the global fixture cleanup for this empty-
registry layout. A later clean docs-only checkout may clean up only with unchanged
audited helper/runtime/native hashes; reports retain the original run identity
separately. Every info, volume/unshare query and archive dispatch rechecks the
captured **current invocation** source; missing context or source changes refuse
before entry. The final return proof also rechecks it. Do not edit/switch source
during an invocation. Never delete or rewrite a stale/missing boot marker to bypass refusal.

## Audit result

The audit reduced 558 unit-test methods to 539 across the same 32 files. The
reduction is intentionally modest: the remaining similar-looking tests protect
different safety/compatibility boundaries. It removed repeated
prose/version assertions, a test of its own temporary-directory setup and
installer checks already covered by the stronger src-layout installer contract.
Repeated cache invalidation, pipeline error, API response and filesystem identity
cases are now named data matrices with their positive/negative regressions kept.
Shared runtime modules are imported normally instead of loading another private
copy in each test file. Unused imports were removed from tests/native fixtures.

Parent-process coverage was measured before/after using the same Python 3.13
environment and `branch = True`, `source = src/oc_box,tools`. Every previously
executed source line and branch remains exercised: 5,704 executed lines before
and after; 1,983 branches before and at least 1,984 after. This is about 68%
combined statement/branch coverage across runtime **and host/developer tools**,
not 100% application coverage. It excludes child processes and JavaScript, so
coverage equivalence is a cross-check, not the sole reason to retire a test.

## Ownership map

All unit-test files were inventoried against their behavior/risk and coverage.
The following groups remain intentionally distinct; an inner validator test and
a CLI abort-before-deletion test protect different boundaries.

| Files | Responsibility / audit decision |
| --- | --- |
| `test_backup_archive` | Keep traversal, hardlink/symlink, special-file, size, ownership and permission defenses. |
| `test_backup_crypto` | Keep real age/envelope, private-memory and TTY contracts; consolidate stage/category/redaction failures into one matrix. |
| `test_backups` | Keep full/portable transport, activation races, rollback/ownership, source preservation, privilege/environment stripping and no-unlock guards. |
| `test_completion` | Keep parser-driven completion and real Readline behavior; they exercise different interfaces. |
| `test_dependencies` | Keep read-only doctor and feature-specific fail-before-change guards; distro installation is a separate CI layer. |
| `test_documentation` | Keep executable config examples, parser/reference coverage, generated source/rendering/reproducibility, privacy and installation contracts; remove repeated prose assertions and arbitrary paragraph lengths. |
| `test_global_vault` | Keep policy/version precedence, dynamic selection, opt-out/overrides, authenticated content, size budgets and cache/error cleanup. |
| `test_global_vault_launch` | Keep first-owner/import/reset/attach ordering; remove setup self-test. |
| `test_global_vault_host_fixture` | Keep fixture isolation, stale-boot/selector guards, first-failure tracing, refusal-before-replay and immutable evidence; these protect destructive host tooling. |
| `test_host_plugin_migration`, `test_native_host_port`, `test_plugin_compat` | Keep normal-host/copy compatibility boundaries, refusal, media/UTF-8 preservation, SDK/schema and reload regressions. Do not infer real host enforcement from mocked sandbox engines. |
| `test_oc_box` | Keep launcher/container/config/limits/history/reset/purge behavior; remove the weaker duplicate installer smoke. |
| `test_opencode_compat` | Keep major/version routing, private auth/env transport, migration, database preservation and SDK cache regressions. |
| `test_packaging`, `test_src_layout` | Keep console/build-channel and src/legacy/editable/resource contracts; the src installer and artifact checker own runtime-file completeness. |
| `test_podman_options` | Keep literal argv, managed-option rejection, SELinux/device diagnostics, command scoping and reset persistence. |
| `test_process_io` | Keep large regular-file capture and cwd/PWD coherence; private capture/redaction has separate credential contracts. |
| `test_secret_prompt` | Keep editor/paste/byte-limit and real TTY/echo/cancellation/resize regressions, not just masked rendering. |
| `test_session_import` | Keep exact transcript/ID, namespace/origin, duplicate/no-overwrite, known normalization and failed-import recovery regressions. |
| `test_session_sync` | V2 paginated root/child discovery, missing-source reconciliation, independent relationship provenance, export completeness and verified host archive replacement. |
| `test_session_terminal`, `test_terminal`, `test_workspace_service` | Keep persistent versus maintenance-shell authority, frame validation, client leases, auth/env isolation and lifecycle locks; consolidate API response outcomes. |
| `test_stale_workspaces` | Keep nondestructive purge/provenance and abort-on-failed-sync/copy recovery boundaries. |
| `test_startup` | Keep warm cache adoption, SDK/lock/config safety and TUI relay; consolidate cache invalidation causes with fresh fixtures per row. |
| `test_status` | Keep honest unavailable metrics, no foreign/project scans, selector behavior and read-only output. |
| `test_updater` | Keep anonymous verified downloads, atomic release switching, failure/timeout/no-downgrade, opt-in scheduling, old callers and bootstrap isolation. |
| `test_v2_host_fixture` | Keep synthetic provider/version/EOF, store/temp/restore/downgrade/evidence safeguards; offline checks are not lifecycle acceptance. |
| `test_vault` | Keep crypto/private files/TTY, explicit named policy, no secret argv/disk/env, no-unlock and fail-before-reset/activation; name the default-env contract explicitly as V1. |
| `test_version` | Keep full-history/legacy identity, installed provenance and licensing; consolidate missing/invalid metadata fallback. |
| `test_workspace_identity`, `test_workspace_repair` | Keep generic VFS capability/fallback, read-only versus locked upgrades, marker/ownership/identity recovery and no replacement/deletion. Consolidate strong-identity mismatch cases, including inode-256 subvolume and invalid-baseline regressions. |

## Reviewing future changes

1. Identify the user-visible contract or named bug a test protects and its owner.
2. If it duplicates an existing test, move any unique input/assertion to that
   owner before deleting it. Give matrix rows explicit names and fresh mutable
   fixtures; do not combine unrelated behaviors into a giant test.
3. Preserve both success and failure paths, timing of refusal, cleanup and
   byte/message/permission invariants. Assert effects, not just mock call counts.
4. Compare coverage and rerun native/process/package layers affected by the edit.
   Do not hide coverage loss by adding exclusions or waiving critical guards.
5. Do not delete a supported V1/legacy/restore regression just because its input
   is old, or relabel previous host failures as accepted results.

Run the standard commands in `CONTRIBUTING.md`. For an optional coverage audit,
use `uv run --no-project --with-editable . --with cryptography --with coverage`
and `python -B -m coverage run --branch --source=src/oc_box,tools -m unittest`
with `discover -s tests`; store coverage data outside the project/HOME. Coverage
is a developer tool, not a runtime dependency or replacement for native tests.

### Host configuration migration

Use synthetic XDG config/data roots for `backup --host` and `restore --host`.
Exercise a host-only real age round trip and a combined full workspace round
trip at mocked Podman boundaries. Verify exact oc-box config and encrypted
vault bytes, native profile opt-in, permission tightening, absence of
plaintext in the outer archive and fixed partial-restore reporting. Cover
malformed payloads, cross-scope paths, changed destinations and no overwrite.
Native V2 credential verification must use disposable credentials and a fresh
native store; an offline fixture does not establish host lifecycle acceptance.
The native smoke runs against the pinned regression binary and the current
stable OpenCode 2.x in CI. Use synthetic unfamiliar 2.x releases to check that
eligibility depends on the round trip, and reject unexpected persisted work
before import and before publication. This does not broaden live history
recovery's version policy.

For changes to the backup transport or restore lifecycle, repeat the
[disposable full/portable Podman fixture](../CONTRIBUTING.md#live-backuprestore-validation)
on the frozen source, from an ordinary non-root Linux host terminal. For
changes to host migration or its dashboard path, also verify the following
on that same source:

1. Use a disposable host user or VM and fresh private synthetic XDG, registry,
   workspace and native auth stores. Record the clean source SHA, runner
   identity, installed version, Linux/Podman/age/native OpenCode versions and
   the isolation used. Do not copy real settings, vault entries or credentials.
2. Create and inspect a host-only archive in the dashboard using the private
   passphrase prompt. Restore host settings to a fresh scope without needing
   Podman. Check exact settings and encrypted-vault bytes and that the native
   profile/store remains absent without its opt-in. Repeat with an occupied
   destination and verify nothing was overwritten. For recipient-mode creation,
   verify that the final review shows all selected public recipients.
3. Use a fresh synthetic running workspace and a private synthetic V2 export
   for a combined archive. Try declined and incorrect native confirmation
   before an approved restore to a fresh destination. Verify fresh workspace
   IDs, retained project/history, a stopped restored container, exact settings,
   and native credential retention by internal comparison only. Keep passphrases
   and exported values out of the report.
4. On another disposable destination, cancel or exercise a controlled refusal
   after the workspace restore but during host application. Check the stopped
   workspace separately, the bounded partial-host report and absence of an
   overwrite/retry. Verify that changing the selected archive clears its
   privileged restore switches and that the dashboard offers no archive deletion.

Report failed or unperformed cases separately; do not infer dashboard or live
host acceptance from mocked Podman or native auth smoke tests.
