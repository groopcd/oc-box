# Plan: exit history sync times out, explicit sync succeeds

Status: planning only, 2026-11-11. Implementation and execution of tests are
deferred to the next work session. No runtime behavior changes in this commit.

Update, 2026-11-12: investigation started; the original plan below is retained.
The diagnostic-first change does **not** yet fix native job cleanup or establish
the reported workspace's exact trigger. See the progress section at the end.

Branch: `fix/exit-sync-activity-timeout`, based on
`8ac68294d1d607ca9ff57fc5e15d16292ff957c6` (`feature/release-1.7`).

## Report and objective

Reported installation: oc-box `1.6.58+g3138a87`, native OpenCode `2.2.7`.
After an all-day session, normal client exit spent exactly 30 seconds in history
sync, then reported `history wait failed (activity-timeout)`, retained container
data, stopped the container, and returned nonzero. A subsequent `oc-box sync`
succeeded after 75.8 seconds and reported one session with superseded unfinished
assistant messages omitted by native export and recorded with a count annotation.

Goal: make ordinary exit synchronize settled history reliably, without requiring
a manual retry or accepting history while work is still running. Preserve safe
pending/error behavior whenever completeness or quiescence cannot be established.

## Findings so far, not a confirmed root cause

- `src/oc_box/oc_box.py`: `launch_client()` waits for attached clients, then calls
  `workspace_service.wait_history()` against the existing private owner before
  `sync_sessions()`. A readiness refusal skips that entire sync attempt. The owner
  subsequently shuts down and `launch_environment()` stops the container.
- `src/oc_box/workspace_service.py`: `wait_history()` has one 30-second monotonic
  deadline covering sequential native CLI/API requests and polling sleeps. It
  requires two consecutive empty foreground-activity/background-job pairs. Slow
  requests can exhaust this budget even if their returned inventories are idle.
- Every background-job status blocks readiness, including terminal statuses:
  an unconsumed completion notice can wake foreground execution again. Terminal
  status, age, or unchanged IDs are not proof that a job is safe to ignore.
- Explicit sync takes a different path in `launch_environment()`: it calls
  `sync_sessions()` without an owner activity wait. `run_v2_history()` and
  `opencode_v2_io.main()` use fresh private services when there is no live owner
  endpoint. Thus the retry does not observe the original owner's in-memory state.
- `src/oc_box/opencode_v2_io.py`: `checked_export()` independently refuses running
  shell/compaction messages, unfinished assistant tails and export/projection
  mismatches. The superseded-assistant exception also checks source-session
  activity after export. Ordinary exports do not provide an equivalent global
  foreground/background-job readiness fence.
- The omission notice is produced during export, not by the activity waiter;
  it does not establish why exit timed out. The retry's 75.8 seconds includes
  discovery, service startup, export, host import and verification; it does not
  establish a required activity-wait duration.

Open hypotheses: genuinely draining work exceeded the deadline; the long-lived
native owner retained stale activity/unconsumed jobs; or repeated native CLI/API
startup/request latency exhausted the shared budget. OpenCode 2.2.7 must be
examined directly; existing native evidence largely targets 2.2.6. Do not call
this an upstream regression or promise a fix before reproducing it.

## Implementation sequence for the next session

1. **Establish a synthetic reproduction and distinguish the hypotheses.**
   - Recheck branch/working-tree state and record exact source/native identities.
     Do not read, edit or publish unrelated private reports or real histories.
   - Extend existing fake-clock cases to model quiet but slow inventory queries,
     actual activity lasting beyond 30 seconds, permanently retained jobs, and
     successful later explicit sync after owner teardown.
   - With private synthetic native state, compare OpenCode 2.2.7 with retained
     2.2.6 behavior: foreground work, background completion-notice handoff, repeated
     turns/interruptions, owner lifetime and per-query latency. Use bounded runs;
     any longer soak is a separately recorded test, not an all-day claim.
   - Capture only fixed phases/categories, bounded aggregate counts and elapsed
     durations needed to distinguish request-budget exhaustion from observed busy
     state. Never log response bodies, IDs, prompts, commands, credentials or raw
     errors. Keep diagnostics in the existing privacy model.
2. **Choose the smallest fix supported by those observations.**
   - If transport overhead is responsible, investigate a bounded private query
     path that avoids repeated native CLI startup while retaining authentication,
     endpoint validation, request deadlines and inventory validation.
   - If legitimate settling exceeds the budget, choose and justify a finite wait
     policy from measured behavior. A larger timeout alone does not repair stale
     activity and must not become authorization to export busy history.
   - If native state is demonstrably stale, establish a supported way to resolve
     it without dropping genuine work. Do not silently clear/ignore jobs, kill or
     restart the owner, or automatically imitate the stop-and-retry workaround.
     Any lifecycle recovery proposal needs separate safety review first.
   - Decide whether improved fixed-category timeout diagnostics should ship;
     preserve existing callers and documented failure/exit-code contracts.
3. **Implement and review regression coverage with the targeted change.**
   - Prefer extending named matrices in `tests/test_workspace_service.py` and
     `tests/test_session_sync.py`, not duplicating existing test implementations.
   - Extend `tests/smoke_session_sync_v2.py` / `tests/smoke_v2.py` for the native
     reproduction where appropriate; retain V1 migration/preservation coverage.
   - Update `docs/cli.md` and `docs/opencode-2.md` if wait/cleanup/diagnostic
     behavior changes, and `docs/testing.md` for evidence and scope. Regenerate
     manuals with `python3 tools/build_docs.py` when CLI documentation changes.

## Required regression assertions

- Quiet slow requests and real busy inventories are distinguishable; all waits
  remain finite, respect remaining budgets, and never query after expiry.
- Two quiet pairs remain required across background notice/foreground handoffs.
  Running, completed, error and cancelled jobs cannot be ignored because they
  are old or unchanged. Idle persistent PTYs remain nonblocking.
- No export/import or synchronized checkpoint advancement on a readiness refusal;
  prior archives/data survive, the result is nonzero and no success is printed.
- When the reproduced condition safely clears, normal exit completes its sync
  without a second command; genuinely pending work still refuses safely.
- Running shells/compactions, unfinished tails, malformed responses, active
  superseded assistants and changed/omitted exports retain their refusals.
- Settled superseded history still mirrors native export with count-only
  annotation. Pending-session isolation, independent progress, import verification,
  child/archive provenance and later retry recovery remain intact.
- Attached TUI/web/terminal/sharing leases still delay shutdown under the lifecycle
  lock. No credential refresh/unlock on attach or vault unlock on history-only
  sync; preserve existing native saved/OAuth auth preparation. No host shared
  service, secret-bearing logs/argv or persistence, or V1 behavior changes.

## Validation and handoff gates

No tests or live diagnostics were run for this planning commit. For implementation:

1. Run focused unit modules, then the documented full unit suite and
   `tests/smoke_mainline.py`. Run native V1/V2 coverage using recorded executable
   versions, including the reported 2.2.7. Review the intended diff and whitespace.
2. Freeze a clean candidate with exact hash/count and native/harness identities.
   Coordinate tests with edits; do not claim a run validates a changed revision.
3. On an **external unprivileged host only**, run the focused history/readiness
   harness against that candidate from a separate harness checkout. Preserve all
   42 current checks and ordered exits `[0,0,0,1,0,0,0,0]`; add separately named
   reproduction checks without relabeling historical evidence. Exercise normal
   exit and explicit retry, large synthetic history, long-lived activity, and
   foreground/background-job handoffs with 2.2.7.
4. Repeat relevant global-vault and full V1/V2 lifecycle acceptance on the same
   integrated candidate before stable publication. Any interactive observations
   remain explicitly manual; offline/native checks are not Podman acceptance.

Do not run host Podman fixtures inside oc-box, use real projects/credentials,
move retained storage or overwrite evidence. Keep unrelated untracked files
untouched. This branch is not a release, and the published 1.6.58 evidence does
not validate a future change. Resume with reproduction/diagnostics, not a blind
timeout increase or export-safety bypass.

## Progress: reproduced mechanism and remaining repair

Private native experiments on unmodified 2.2.7 and 2.2.6 reproduced a retained
background-shell job after location reload. With zero foreground sessions, the
cancelled row remained across shell release and a real 30-second production wait.
Native export was still accepted; reopening could expose the retained row as
running. Explicit sync success therefore does not imply the row disappeared.
Ordinary interrupted jobs recovered when their synthetic work completed. Idle
native query timing and held-stdin checks did not reproduce those alternatives.
This establishes a mechanism, not the trigger of the reported real session.

Read-only review of public native v2.2.6 source at
`dc2ffcf8bf26f9e669b6aa22b95f50e12be15e06` corroborates that mechanism:
`packages/core/src/job.ts` retains global jobs, persists running background rows,
does not persist cancelled settlement, and removes rows after completion notice
admission. `packages/core/src/tool/plugin/shell.ts` delivers that notice through a
location/plugin-scoped observer; location teardown can interrupt it while the
global job survives. `packages/core/src/job/list.ts` uses live generation state
when present, otherwise durable state. The public job/shell schema exposes no
definitive observer-liveness, notice-admission or orphan marker. This source review
is of 2.2.6; compiled 2.2.7 behavior was measured separately, not assumed identical.

Independent safety review initially rejected a naive post-owner snapshot fallback.
Host-wrapper teardown alone is not proof that all remote native
processes stopped, a `sync_sessions()` call can open multiple private services,
and an `activity-timeout` can also mean an API failure at the deadline. None of
those conditions authorizes new concurrent database access or ignoring work.

The first runtime change adds only fixed-phase and capped count-only timeout
diagnostics while preserving the original exception text, readiness rules,
deadline, failure status and cleanup. Unit coverage distinguishes unavailable
partial observations from a complete empty observation and keeps retained jobs
blocking. Public documentation distinguishes readiness refusal (no final exports)
from a pending export (other sessions still progress).

The first-stage repair gate was to confirm the mechanism on a disposable
host reproduction or an operator-provided count-only recurrence, then establish a
supported native cleanup/quiescence contract. Do not silently reinterpret
cancelled/running retained jobs as idle, rely on restarts clearing them, or ship
the diagnostic as a completed fix. Fresh external-host acceptance remains pending.

## Subsequent implementation: fenced post-stop snapshot

Continued independent review established a narrower useful contract without
changing native job state: let the live owner refuse, close it, verify an actual
whole-container stop, then permit one non-executing persisted-history snapshot.
The same existing ready container is pinned by immutable ID, passive entrypoint,
project/home association and ownership. No replacement/bootstrap/preparation is
allowed. Private stop intent plus stable admission serializes the sole restart
against this launcher's explicit stop; indeterminate starts retain a persistent
fence rather than permitting a false successful-stop claim.

The final live request's phase cannot determine snapshot eligibility: native
CLI-query timing otherwise makes it random. Two complete validated foreground-empty,
shell-only pairs are a routing hint. A real deadline timeout can retain them;
known non-timeout API errors and contradictory/malformed late responses cannot.
All known shell statuses are included because a cancelled row can hydrate as
running on the next owner. The wait still fails and **no live export** is waived.

One isolated reader supplies the whole snapshot. It forwards no vault/provider
environment, uses no saved/project integrations and performs no credential refresh.
Source/destination native scheduler rows and nonempty system configuration cause
refusal before projection. Disabling schedules alone is insufficient: native
`Schedule.rewake` can still wake durable input. Existing checked exports and host
verification remain mandatory; no row, outcome or notice is manufactured or
deleted. The snapshot budget is 180 seconds, with separate transition/cleanup
allowances. Even successful snapshots retain the original nonzero/interrupted-work
warning, and cleanup stops the container again.

The new native fixture exercises the reader and real archive round trips on
2.2.6/2.2.7 behind a **local process barrier only**. Whole-container stop/death,
same-ID restart and concurrent explicit-stop authority require the focused
external-host gate on a clean frozen candidate. No host fixture may run here;
no stable publication is authorized by these offline results.

## First external attempt: transition oracle failure

The first snapshot gate on runtime `d736bc1` reached the real eligible timeout and
old-owner closure, then failed the strict transition assertion. This is failed
evidence; it does not establish snapshot or stop-cancellation acceptance.

Source inspection and an offline regression exposed a fixture-only phase error:
using `DEADLINE` as a recovery marker also includes the separately bounded outer
cleanup. The corrected v2 harness instead forwards the actual recovery call,
tracks its thread/return value, and retains the original strict seven transitions
and two stop proofs. An unexpected extra operation inside that call still fails.
The old report lacks the event trace, so another runtime issue is not ruled out.

No runtime source is changed for this oracle correction. Keep the failed store
and old harness intact; use a new external harness file and fresh fixture roots
against the unchanged clean `d736bc1` candidate. Record the new runner identity
separately, and do not retrospectively relabel the first run.

## Reviewed focused host results

The two fresh v2 reports now pass metadata and independent review: runtime
`d736bc1bb33ffeedee65ad3ec281de77c1e31847` / `1.7.86+gd736bc1`, count 115,
clean source, all 44 audited hashes matching Git, runner `60df669` and native
2.2.6. Snapshot passed all 12 checks with one restart/reader and completed
snapshot; stop cancellation passed all six with no restart/reader and explicit
stop exit 0. Both observed 34 cancelled-job pairs, retained owner exits `[0,1]`,
made no preparation/authentication calls during recovery, and had no primary or
secondary failures. Exact evidence hashes and scope are in `docs/testing.md`.

This accepts the focused snapshot/stop-suppression contract only. The first v1
run remains failed; no native job is repaired and interrupted execution is not
declared successful. Raw reports remain private/untracked. The runtime bytes are
unchanged by the subsequent harness correction and this evidence note.

The next host regression gate at that stage was the retained default 42-check
history/readiness run on the same frozen source, with exits `[0,0,0,1,0,0,0,0]`. Broader lifecycle,
vault, additional fault/race and integrated release gates remain separate before
stable publication; native 2.2.7 currently has offline reader coverage only.

## Reviewed baseline and fix-branch handoff

The separate baseline report is now reviewed: all 42 expected checks are true,
ordered owner exits are `[0,0,0,1,0,0,0,0]`, no primary/secondary failure or trace,
and source/runner/native identities exactly match the two focused reports. Its
byte hash and bounded scope are recorded in `docs/testing.md`.

The fix branch therefore has 60 reviewed scripted synthetic host checks on the
frozen `d736bc1` runtime with native 2.2.6. Runtime/donor bytes remain unchanged
through the subsequent fixture and evidence-only commits. The original failed
recovery attempt remains failed and every raw report/store remains private.

The requested fix-branch implementation and focused regression validation are
complete. Integration into `feature/release-1.7` needs explicit approval beyond
this branch's scope; do not merge into `master`, tag or publish a stable release
on this evidence alone. Lifecycle/vault/backup, interactive/integrated release,
additional fault/race and native 2.2.7 host validation retain their own gates.
