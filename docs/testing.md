# Test strategy and suite audit

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

Run acceptance fixtures without Python optimization. In particular,
`tools/test_backup_podman.py` relies on assertion oracles and refuses `-O` and
`-OO` before application imports, host probes or fixture allocation, including
for `--help`. Its offline entrypoint tests verify this ordering; they do not
execute or establish a new host backup/restore acceptance run.

### V2 session-history synchronization

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

Use a regular unprivileged host terminal, outside containers and shell sandboxes.
Only fresh synthetic projects, config/history and a verified private rootless
Podman store may be used. The existing global-vault fixture's selectors, boot,
store, ownership and mount checks remain authoritative; a history-purpose marker
and source-workspace allowlist add restrictions rather than bypassing them. Do
not reuse a completed global-vault/lifecycle root for these cases.

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

Published `v1.6.51` remains unchanged. Its accepted host reports predate the
superseded-assistant correction; the correction's native/offline pass is not new
host acceptance. Freeze a clean candidate containing the runtime fix, regression
fixtures and documentation, then run all three host fixtures on that exact
revision before the next stable GitHub/PyPI publication.

The focused V2 runner retains its original 27 checks and adds six pending-shell
checks: all **33** must pass, including same-pass progress on both sides of a
pending session, exact fixed reasons and unchanged pending archives/checkpoint.
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

After acceptance, follow the
[publication checklist](../CONTRIBUTING.md#release-gate-and-publication-checklist):
fast-forward the accepted commit without changing its identity, publish the
matching GitHub Release/PyPI version, and compare the public artifacts' hashes.
No new host pass or release is recorded by preparing this handoff.

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
a clean full-history source checkout, explicit unmodified OpenCode 2.2.6 and an
existing disk-backed work parent. Never run it inside oc-box or reuse completed
fixture roots. No real project, operator credentials or vault unlock is needed:
the owning start explicitly uses `--no-vault` and a synthetic `_API_KEY`.

```bash
python3 -B tools/test_interactive_podman.py start \
  --work-dir /path/to/fixtures --opencode-v2 /path/to/opencode-2.2.6
```

Optional `--temp-dir /path/to/staging-parent` changes only private temporary
placement. Both work and staging parents must be outside retained global-vault,
V1/V2 and backup fixture roots and their descendants, including renamed roots
with fixture evidence markers. No fixture files/store are moved or removed.
The foreground owner prints its fresh exact root and secondary commands;
wait until its TUI is ready, then use those commands in other host
terminals. Only the single synthetic `source-a` workspace is allowed:

```bash
python3 -B tools/test_interactive_podman.py tui --fixture /path/to/printed-root
python3 -B tools/test_interactive_podman.py terminal --fixture /path/to/printed-root
python3 -B tools/test_interactive_podman.py web --fixture /path/to/printed-root
python3 -B tools/test_interactive_podman.py stop --fixture /path/to/printed-root
```

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
guards and native relay dispatch, not real Podman/UI acceptance.

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
