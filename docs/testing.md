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

### Update-channel gates

Stable/devel select published releases, not moving branch HEAD, across both
GitHub/curl and package-owned installations. Preserve separate coverage for:

- CLI mutual exclusion, upgrade-only selectors (including implicit start),
  read-only `--check`, channel dispatch and root/container refusal before any
  network, manager, or preference changes (`tests/test_upgrade_cli.py`).
- Anonymous bounded release discovery, final/prerelease classification, highest
  public-version selection, tag/commit/branch verification, no silent downgrade,
  explicit downgrade warning, unchanged preferences on failure/preview, and
  saved per-owner channel choices. Keep old updater/installer bridge callers.
- Verified pipx/uv/dedicated-venv ownership, ambiguous/system/user-site/editable
  refusals, supported-extra retention, manager failure, same-project `.dev0`
  selection, no GitHub installer or GitHub-setting changes from package paths,
  and disabled automatic package updates. Ownership/discovery imports stay
  stdlib-only.
- Bootstrap default/explicit channel selection, selector conflict,
  `--with-vault`, anonymous clean environment and target-supported extras;
  stable 1.6 must not acquire a development-only `--with-tui` requirement.
- Completion/parser parity and generated manuals, wheel/sdist source identity,
  matching final/prerelease tags and publication branch checks.

The initial channel-capable stable 1.6 release belongs on `master`; later 1.7
development releases belong on `devel` without merging that line into master.
An explicit program downgrade is not workspace/session rollback. Test it with
private synthetic installations, never by resetting a real workspace.

Channel publication retains every existing source/package/native, build-only,
applicable seven-distro and external-host gate in the
[publication checklist](../CONTRIBUTING.md#release-gate-and-publication-checklist).
For runtime/history/lifecycle/credential changes, obtain relevant fresh host
evidence on the clean integrated candidate; a development prerelease is not an
exception. Channel fixtures, build-only artifacts and unit/native results add
no live Podman acceptance. Earlier host evidence below stays unchanged.

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

#### Post-stop persisted-history snapshot candidate

`history_recovery.py`, `history_reader.py` and `workspace_control.py` add a one-shot
snapshot path after eligible shell-only timeouts. The ordinary live wait still
fails. Eligibility is based on two prior complete validated pairs, not the final
query phase: a tiny remaining CLI-query budget must not randomly disable the
snapshot. A real timeout may retain those observations, but contradictory late
responses and known non-timeout API failures invalidate them. All versions/kinds
outside the supported 2.2.6/2.2.7 shell case retain the existing pending behavior.

Unit/process matrices cover private stop intent before admission, stable locks,
uncertain transition fences, immutable resource identity, stop/start/snapshot/stop
ordering, one isolated reader, deadline propagation and unchanged nonzero results.
Malformed/unsafe controls never initialize or repair themselves from readers.
Source and destination scheduler guards inspect metadata only, use WAL-visible
read-only SQLite connections and refuse every schedule row/unknown schema. The
native scheduler's rewake path can run despite `-schedules`; no-schedules metadata
and system-configuration guards are therefore prerequisites, not optional hints.

`tests/smoke_history_recovery_v2.py` runs on unmodified 2.2.6 and 2.2.7. It closes
the old **local native process**, then exercises the production reader, real
archive/import verification, retained running-looking job metadata, no invented
notices and source/destination scheduler refusals. Provider/shell/plugin canaries
must not execute during the reader. It is included in `tests/smoke_v2.py`; it does
not establish whole-container process death, actual rootless stop/start ordering
or host acceptance. The original live timeout and nonzero result remain distinct
from a successful checked snapshot; native job cleanup is still not repaired.

The focused host runner adds opt-in `--history-recovery-case snapshot` and
`--history-recovery-case stop-cancel`. Each invocation creates a separate fresh
synthetic root. Their `oc-box-history-recovery-host-v2` reports require 12 and six
checks respectively, owner exits `[0,1]` in each, and explicit-stop exit `0` in the
cancellation case. They are **additional to**, not replacements for, the stable
default 33-check/five-exit run. The new cases remain strictly native **2.2.6**;
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
publication and integration/release approval remain separate; later evidence-only
commits do not extend the recorded tested source identity.

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

### Parented-readiness harness correction

The first external current-native baseline attempt on clean
`9a6e99ad01b3bf02e19edb3ec9d7ab3289d40f22` / `1.6.94+g9a6e99a`,
runner SHA-256
`96ae30ae0b6e9b726443c507ca17e128bb9ef91bb1dab8a751ceaa06f8516087`,
failed during readiness with primary and secondary `worker-timeout`.
Its report records 14 of 33 checks and only the graph launcher exit zero.
It remains **failed evidence**, not partial acceptance of the full baseline.
The retained store/report must not be moved, overwritten or replayed.

A separately identified private native reproduction used the unchanged
readiness method, real CLI/API inventories and production history waiter.
Native 2.3 completed the child, projected a subagent-completion notice into
the parent and woke the parent's foreground execution. The old one-request
provider contract rejected parent requests before headers, causing retries
while the real global waiter correctly remained pending. The fixture's
60-second join could expire before the waiter's unchanged 90-second deadline.
The unparented 2.3 and parented 2.2.6 controls did not exhibit this mechanism.

The host report did not record final inventories/provider counters, so the
private reproduction is a matching causal mechanism—not a retroactive direct
observation of that host run's internal state or new host acceptance.

Correction work belongs in a separately identified **harness checkout**,
outside the still-frozen application source. Require strict version-specific
parent-completion handling and genuine child/parent activity observations,
unchanged production quiet/job/export guards, and bounded fixed diagnostics.
The background stage also has a parented child; verify its actual native
notification ordering rather than silently treating a parent request as a
child phase. Do not remove parenting or merely increase a timeout to obtain
success.

Before a fresh host run, validate the corrected native readiness/background
methods on 2.3 and legacy 2.2.6 and review the source/runner hashes separately.
Keep the failed attempt and all previous evidence intact. Any passing
preparation is local native/process evidence only; the complete source-bound
host gates remain required.

The corrected native 2.3 report retains its original **33 baseline checks**
and additionally requires **12 structured child/parent readiness checks**.
Foreground requires one child and one parent provider request; background
requires three child and two parent requests. Genuine held activity and
two final quiet pairs remain mandatory. The legacy 2.2.6 contract stays at
one foreground plus three background requests without parent wakeups.
Unexpected, duplicated or mismatched parent notices remain failures.

Use the separate readiness probe to validate the corrected runner against
the still-frozen runtime, with both native-created and seeded parent modes:

```bash
python3 -B tests/smoke_session_sync_readiness_v2.py \
  --source /path/to/clean/frozen/source \
  --expect-commit 9a6e99ad01b3bf02e19edb3ec9d7ab3289d40f22 \
  --opencode /path/to/opencode2 --native-version 2.3.0 \
  --parent-mode native
# Repeat with --parent-mode seeded; use 2.2.6 for the legacy native control.
```

The probe adapts only local execution/addressing, FIFO placement and explicitly
limited lease/archive callbacks. It neither runs Podman nor establishes owner,
lifecycle, archive or release acceptance. Reports bind source, runner, probe and
native hashes and include fixed-schema failure observations.

### Current native 2.3.0 profile

The current 1.6 candidate extends the isolated history reader to the exact
native release **2.3.0**, while retaining 2.2.6/2.2.7 reader regressions and
legacy V1 native CI. This section defines preparation and required host gates;
it records **no new external-host acceptance**.

Use the explicit `--native-version 2.3.0` profile with the user's unmodified V2
executable. The version, resolved path and hash are recorded, and retained
retries must match their original profile. Omitted profile arguments preserve
the old 2.2.6 default; they do not mean "whatever 2.x is installed". Never
change a donor constant or use a V2 executable as a fake V1.

Local native preparation distinguishes two recovery cases:

- 2.2.6/2.2.7: the original cancelled-job-after-reload case remains unchanged.
- 2.3.0: a real held background shell produces an actual ordinary wait timeout;
  the private producer and shell are stopped/reaped before the native reader.
  Complete post-barrier job, schedule, message and part rows are compared after
  reader startup/export/shutdown. Eight source and eight destination scheduler
  refusals include unknown statuses and committed WAL rows.

The 2.3.0 local barrier is not whole-container evidence. Its source-bound host
profile separately requires the real shell-only timeout, immutable-resource
stop/restart, one reader/snapshot and unchanged execution counters. The stable
baseline remains **33 checks**, five stages and exits `[0,0,0,1,0]`; the
**12 snapshot** and **six stop-cancellation** checks remain separate. A drained
reload observation is never relabeled as the older retained-job reproduction.

From a normal unprivileged host terminal, outside containers/sandboxes, use
separate full-history clean candidate and harness checkouts and fresh
disk-backed fixture/temp parents:

```bash
uv run --isolated --no-project --with cryptography python -I -B \
  /path/to/harness/tools/test_session_sync_podman.py \
  --source /path/to/clean/candidate \
  --expect-commit FULL_REVIEWED_CANDIDATE_COMMIT \
  --opencode-v2 /path/to/opencode2 --native-version 2.3.0 \
  --work-dir /path/to/fresh-fixtures --temp-dir /path/to/private-temp
# Repeat with --history-recovery-case snapshot, then with stop-cancel.

uv run --isolated --no-project --with cryptography python -I -B \
  tools/test_global_vault_podman.py \
  --opencode-v2 /path/to/opencode2 --native-version 2.3.0 \
  --work-dir /path/to/fresh-fixtures --temp-dir /path/to/private-temp

uv run --isolated --no-project --with cryptography python -I -B \
  tools/test_v2_lifecycle_podman.py \
  --opencode-v2 /path/to/opencode2 --native-version 2.3.0 \
  --work-dir /path/to/fresh-fixtures --temp-dir /path/to/private-temp
```

The global-vault and V2-only lifecycle invocations run from the pinned candidate
checkout. The
current-V2 lifecycle gate additionally requires full **and portable** restore,
same-session/history preservation, reset/OS distinction and wrong-passphrase
refusal before deletion. Global-vault full restore alone is insufficient.

The lifecycle profile emits its own V2-only report with ten named check groups.
It has no V1 requirement, no resume/retry mode and no claim that the old
dual-major six-stage fixture ran. Retain the original failed report/store and
start a separately identified fresh run after diagnosis; do not overwrite
encrypted evidence. `--cleanup` validates only the recorded private fixture
and stopped, owned resources; it is not general storage cleanup.

Keep the old dual-major lifecycle fixture for legacy migration/downgrade work.
A current V2-only report must explicitly exclude unrun V1 migration/reopen and
downgrade checks, interactive observations, real providers/projects, actual
OAuth refresh and security/network-containment certification. Existing legacy
reports and all earlier failures retain their original identities and scopes.

### Reviewed frozen-source 2.3 host reports — lifecycle still blocked

Fresh, separately retained host runs were reviewed on unchanged application
`9a6e99ad01b3bf02e19edb3ec9d7ab3289d40f22`,
`1.6.94+g9a6e99a`, count **123**, clean full history. The history runner came
from separate harness commit `14357e8f23ab7ada1cbc373f440bc9697a25a305`,
SHA-256
`3eb6af9b5bf2e8b5d838d83f6e4b89ffbadd191359454a349c58f3b85f4776cb`.
Every report records unmodified native **2.3.0**, SHA-256
`2e0e4656521ca56e5ba16a00d48429958b8105b3fa6466710a4350e68759381f`.
These are reviews of user-supplied external-host evidence, not local Podman
runs or acceptance inferred from native callback rigs.

| Report | Original file SHA-256 | Reviewed result |
| --- | --- | --- |
| Baseline history/readiness | `71d44cb18a8dfec8ee16110c1ea523510c0021223f9685d7e1777d4f62e060f3` | **33 original + 12 structured checks** true; exits `[0,0,0,1,0]` |
| Snapshot recovery | `ff0164bd1367f31361159f7db9dcf59b7fe58d7b301de82471f2a294a79b887c` | **12 checks** true; exits `[0,1]`; restart/reader/snapshot `1/1/1` |
| Stop-cancellation | `b6d627ea4c4bb73dc78f0845e6679fbc4e9a078f847a0b571525d0a38574716a` | **Six checks** true; exits `[0,1]`; restart/reader/snapshot `0/0/0`; explicit stop `0` |
| Global vault | `5998bb8c7030ea78063d2287945ef173406b811d901cf4f7138f24c7867a5799` | **13 stages** passed, including full encrypted backup/restore |
| V2-only lifecycle | `8ad73c75b2b9643626820849365fdcd515d486ba980d4a16f63fcfad78d6c2b7` | **FAILED** with broad `fixture-error`; only private-selector and native-seed checks true |

Locally reconstructed fixed-schema copies exactly match all five original
whole-file checksums. Both primary and independent review verified all **36
runtime/donor hashes** in each history report against the frozen source, and
all **154 full-source hashes** in the lifecycle report. The global-vault
report records source/native identities but has **no runtime hash map**; do
not describe it as a 36-hashed report. Its return-zero evidence comes from
the supplied successful command sequence proceeding to the next gate, not
from a JSON exit-code field.

Baseline workers completed across actual child/background/parent holds,
with two final global quiet pairs in each observation set, foreground provider
requests **two**, cumulative requests **seven**, and no provider/query errors,
failure categories or trace. The recovery reports each record **48 running
pairs and zero cancelled pairs**, no preparation/auth calls, two provider
requests and one shell start. Their `durable_job_retained` label does **not**
establish the legacy 2.2 retained-cancelled-job precondition. Snapshot remains
one non-executing post-stop reader, never a live readiness waiver or native
job-cleanup claim.

Global-vault scope includes scripted masked prompt/TUI exit, delivery and
rotation/reload/attach, native Build permissions, opt-out and full encrypted
restore with stopped byte-exact and latest-host-vault/history/OS checks. It
does **not** replace selected-policy full **and portable** restore/reset
acceptance. The failed lifecycle report establishes neither completed host
import nor owner, restore, reset or source-preservation acceptance.

Static inspection and a bounded synthetic production-client callback probe
found a deterministic lifecycle **harness defect**: its first start omits
`--import-session`, although its callback requires the separate host seed's
session ID and transcript. The passing global-vault fixture explicitly
requests that import. The old lifecycle report discards the fixed refusal
reason; this matching mechanism is **not proof of the exact historical
failure substep or callback entry**.

Keep the frozen application and donor helpers unchanged while correcting
external first-start orchestration. Request the supported seed import exactly
once, never on restart/restore/reset, and retain strict selection/transcript,
ownership, full/portable restore, reset and refusal checks. Require separately
identified fixed primary/secondary diagnostics and fresh source-bound host
validation after correction. Do not replay the retained failed store.

The independent `tools/test_v2_lifecycle_external.py` adapter is preparation
for that fresh gate, not an application update or acceptance of the failed run.
It verifies the immutable candidate before imports and delegates to the frozen
lifecycle helper, adding only the validated first-start import argument.
The external checkout must retain `tests/test_v2_lifecycle_external.py` for
its separately recorded offline-test provenance. Pin and review the external
checkout independently from the application before using this example:

```bash
# Normal unprivileged host terminal only; both parents must be outside every
# retained fixture/store, and the runner must be outside the frozen source.
uv run --isolated --no-project --with cryptography python -I -B \
  /path/to/clean/external-harness/tools/test_v2_lifecycle_external.py \
  --source /path/to/clean/frozen/source \
  --expect-commit 9a6e99ad01b3bf02e19edb3ec9d7ab3289d40f22 \
  --opencode-v2 /path/to/opencode2 --native-version 2.3.0 \
  --work-dir /path/to/fresh-fixtures --temp-dir /path/to/private-temp
```

The adapter accepts only its audited 2.3.0 executable and frozen source.
It has no retained-store inspection, cleanup, retry or legacy mode. Preserve
both the original fresh `result.json` and companion
`external-lifecycle-result.json`. Acceptance requires all ten original checks,
unchanged exclusions, exactly one verified initial import, all five owning
launches completed with no later import, original exit zero and matching report
hash, and shutdown/close/join once each with no alive provider worker. Fixed
primary, cleanup and reporting failures remain separate and must all be absent.
Source/native/runner/offline-test/helper identities are recorded separately.
No public application tag or release assets may be produced from the harness.
The companion is published without replacement only after private staging is
written, flushed, fsynced and closed. Staging is deliberately retained as
non-authoritative evidence; on success both links remain inside the fresh
private root. The original single-link report and frozen reader are unchanged.
Late staging failures cannot publish a final receipt. This is an atomic
publication boundary, not an exhaustive crash-durability guarantee.

Accepted scope is **63 scripted synthetic history/readiness/recovery checks**
plus the separate **13-stage global-vault gate**, not complete release
acceptance, interactive UI, real inference, OAuth, network-containment,
security certification or new legacy acceptance. The original baseline and
lifecycle failures remain failed evidence. **Stable publication is blocked
by the lifecycle gate; application `9a6e99a` and main 1.7 remain unchanged.**

The subsequent external lifecycle attempt used reviewed harness
`d622ee03d1357de4676a4c5c99441f58c85867a6`, runner SHA-256
`3f14f9543696b0dca2b228df668b0ea9d7b4484f5839db6949ad197c12e57f4c`,
and unchanged application `9a6e99a`. Its original report SHA-256 is
`ce8613bb75325e750af09db7bf3b8fe3ac1d00dd84a63aeb4cae8c26f1fb30a1`;
the companion report SHA-256 is
`164fc630cb1952574082d8c06bc2aa7beecb2e40c91802d24454739d1e64ab00`.
Both reports remain **FAILED**, now with fixed category `stopped-restore`.
Five initial check groups completed, including initial import, selected tools
and same-session restart/rotation/attach. Exactly one initial import and two
source owning starts completed; no restored owning start completed. Provider
cleanup finished and primary/cleanup/reporting diagnostics stayed separate.
All 154 reported source hashes match frozen Git. Neither restore kind, reset
nor final source preservation is accepted from that report.

The stopped-restore conjunction does not identify its failed member. The exact
whole-home oracle includes root modes and child special bits, whereas archive
extraction intentionally forces root privacy and strips special bits. Synthetic
proof establishes a conditional oracle incompatibility, **not** that those
conditions existed in this host run. Do not waive content/ownership checks,
infer data loss, or repeat accepted gates merely from that static finding.

`tools/diagnose_v2_stopped_restore.py` is a narrowly report-bound, file-only
reader for this specific failed pair, not a lifecycle retry or engine selector:

```bash
python3 -I -B /path/to/reviewed/harness/tools/diagnose_v2_stopped_restore.py \
  --root /path/to/the/audited/fixture \
  --source /path/to/clean/frozen/source
```

It verifies original file checksums and selected fixture/registry/seed/marker
relationships, requires a valid matching retained boot marker, and emits only
fixed categories and current metadata-comparison booleans. Owned regular files
are pinned before data opens; paths and generic VFS project identities are
verified through directory descriptors. It neither executes selectors, Podman
or OpenCode nor reads home volumes or decrypts backups. Disposable selector
interpreters are not repaired or executed. Stale/invalid/missing boot markers
are refusals, never permission to refresh or move a store.

The historical `saved` home digest was a local variable, not a persisted
baseline. This file allowlist cannot reconstruct it or prove the exact
historical failed predicate, current stop, archive authentication or complete
lifecycle acceptance. Diagnose the one unresolved comparison before deciding
which correction and affected validation, if any, are justified.

A later fresh lifecycle run reproduced `stopped-restore`, with the same two
report hashes above; these fixed reports contain no per-run fixture IDs at that
failure boundary. The original boot-stale store was not replayed. The bounded
reader on the **new** root then reported a matching boot marker and all six
metadata comparisons true: version, fresh workspace/container/home identities,
internal project path and restored last-session selection. Source and recovered
registry selection also agreed with the seed. This narrows the likely
discrepancy to the home comparison but remains **current metadata consistency**,
not proof of historical home contents, root permissions or exact failed member.

Any external whole-home oracle correction must compare the exact restored tree
against the source's **documented archive-policy result**, never normalize both
sides or omit the home check. Preserve all regular bytes, entry inventory,
types, symlink targets, ordinary modes and child ownership; require restored
root privacy and reject unexpected ownership or privilege bits. Source
preservation remains exact, including its original root mode and special bits.
New observations must be confined to fresh, verified-owned stopped resources,
with fixed nonsecret results and no reuse or repair of retained stores. This
does not authorize repeating accepted history/recovery/global-vault gates.

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
