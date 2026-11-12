# Plan: exit history sync times out, explicit sync succeeds

Status: planning only, 2026-11-11. Implementation and execution of tests are
deferred to the next work session. No runtime behavior changes in this commit.

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
