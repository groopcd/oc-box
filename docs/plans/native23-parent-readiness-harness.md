# Native 2.3 parent-readiness harness correction

## Scope

This is a **fixture-only harness correction**, separate from the frozen 1.6.94
application candidate. Do not change the candidate's runtime, donor helpers,
commit identity or previously built artifacts merely to correct this provider
oracle. The main 1.7 branch remains outside scope.

The first external host run failed during readiness after 14 of 33 checks.
Its report and retained private store remain failed evidence; no replay,
cleanup, store movement or relabeling is authorized by this correction.

## Reproduced mechanism

Private native tests against the same frozen runtime and native 2.3 executable
reproduced the original readiness method's timeout categories:

1. A genuine parented child remains foreground-active while its synthetic
   provider response is held.
2. After response release, the child finishes and its native client exits zero.
3. Native 2.3 projects a synthetic subagent-completion notice into the parent
   and wakes the parent's foreground execution.
4. The old fixture accepts only one foreground provider request. It rejects
   subsequent parent requests before response headers, causing native retries
   while the parent remains active.
5. The global production history waiter correctly remains pending. An outer
   60-second fixture join can expire before the waiter's own 90-second deadline,
   hiding its eventual real activity-timeout result.

Unparented native 2.3 and parented native 2.2.6 controls settled with one provider
request and no errors. The exact-method private reproduction also reproduced
the first host report's secondary worker timeout.

The host report did not contain those internal provider or final activity
counters. The private measurements establish a matching causal mechanism,
not a retroactive direct observation of the retained host run.

## Required correction

- Retain the parented-child case and every original readiness check. Do not
  change the child to unparented simply to avoid native 2.3 behavior.
- Legacy 2.2.6 keeps its original one-child-request foreground contract.
- Native 2.3 may accept only the genuine expected parent-completion request,
  bound to the synthetic child, completion marker and projected notice.
  Matching seed text or permitting arbitrary extra requests is not sufficient.
- One real global history waiter must span held child activity and real parent
  wake. It remains pending until both complete, and still requires two actual
  quiet activity/job pairs.
- Validate both child and parent transcripts, exact expected provider counters,
  zero provider errors and native client exit zero before credit.
- Keep production wait budgets and all nonempty-job/foreground/export refusals
  unchanged. Fix only the fixture join accounting so it does not obscure the
  real waiter's deadline or failure.
- Preserve bounded cleanup and original failure identity. Diagnostic fields
  may contain only fixed-category, allowlisted counts and booleans, never raw
  native errors, prompts, session IDs, argv or environment values.

The later background stage also creates a parented child. Its original three
child provider phases must not accidentally consume a new parent request as
the next child phase. Determine and test actual native 2.3 ordering/notices;
keep child background-running, completion-notice, wake and quiet safeguards.

Private native observation found three child and two parent requests in that
background stage. The first parent wake occurs while the shell job is running;
the second follows the child's final acknowledgement. Both projected notices
have `metadata.state = completed`, but their XML states are respectively
`running` and `completed`. The running notice contains the idle marker plus
native's actual shell summary; the final request retains that earlier frame.
Match fresh notices to projected data and the bound child/command/result.
Do not equate metadata state with XML state, require all retained frames to
be completed, or discard prior foreground-stage parent history. These are
private native observations, not host acceptance.

## Validation and host handoff

Native preparation must invoke the corrected readiness/background methods and
real native API/CLI/provider/history waits. The local adapter may replace the
Podman boundary and explicitly limited lease/archive callbacks; it must not
fabricate native activity/status responses or claim host/lifecycle acceptance.

Require negative provider/provenance/extra-request/timeout tests and actual
native 2.3 plus legacy 2.2.6 controls before reviewing/publishing the harness.
Keep runtime and frozen donor hashes identical to the application candidate.
Any CI probe must identify the separate runtime source and harness hashes.

Use a newly created, private host fixture after review, with the frozen source
commit and separately reviewed corrected harness commit. Do not reuse the
failed root. All still-required host gates remain required before stable
publication; a passing native preparation or corrected provider oracle is not
host acceptance.
