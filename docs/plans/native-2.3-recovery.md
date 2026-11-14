# Native 2.3.0 recovery and current-V2 validation

## Status and scope

This is preparation for a new **1.6 stable candidate**, not an accepted host
run or release. The prior candidate and historical reports remain unchanged.
The user explicitly approved extending persisted-history recovery to native
OpenCode **2.3.0**, after learning that its original allowlist covered only
2.2.6/2.2.7. Do not equate adding a version string with establishing support.

The main 1.7 line is in final testing and is outside this work. Keep its source,
branches and publication unchanged.

OpenCode 1.x is legacy coverage. Preserve its existing native migration,
crash-origin, transcript, reopen and compatibility regressions; do not require
a normal installed V1 executable for current V2-only host checks. A V2-only
report must explicitly exclude V1 migration and downgrade rather than mark them
passed.

## Existing evidence does not transfer

The retained 2.2.6 host history/recovery reports identify their original runtime,
runner, executable and check counts. They are not 2.3.0 acceptance and must not
be relabeled.

Bounded private native 2.3.0 preparation against the prior candidate exercised
the ordinary tool/plugin/MCP, credential, transcript, terminal and history-wait
contracts. Its reload case observed drained jobs, not the retained cancelled
job reproduced on older natives. These local native observations establish
neither rootless-Podman lifecycle acceptance nor persisted-reader safety.

## Recovery invariants

Support remains an explicit reviewed-version allowlist, not arbitrary future
major-2 compatibility. Unknown or mismatched native versions fail closed.

The following requirements do not change:

- The original live history wait must actually fail. Never waive foreground
  work, running/terminal jobs, incomplete exports or missing quiet observations.
- Require the old producer to stop, and on the host verify a whole-container
  stop and immutable same-resource restart. A closed local test process is not
  evidence of that host transition.
- Keep lifecycle ownership, stop intent/admission, persistent start uncertainty,
  resource identity and project/home mount checks intact.
- Start exactly one private unregistered reader. Never elect a replacement
  owner, refresh credentials, create resources, or resume a native turn.
- Keep private empty discovery roots, project/system configuration fences,
  restrictive native integration policy, explicit source/destination database
  selectors and bounded control transport.
- Inspect source and destination scheduler metadata before native startup or
  projection. Every schedule row, including paused/terminal/unknown statuses,
  refuses. A disabled feature flag is not proof that scheduler rewake is off.
  Do not hide committed WAL rows or create/migrate a database merely to inspect
  it.
- Verify actual native schema and startup/rewake behavior on 2.3.0. Preserve
  checked-export/import/projection comparisons and host archive verification.
- Do not delete, settle or rewrite historical job rows as a recovery strategy.
  Reader execution must not make provider calls, run shell tools or activate
  user plugins.
- Preserve the original nonzero/interruption result, snapshot warning, bounded
  cleanup and retained-data refusal behavior.

## Version-specific native cases

Keep the original 2.2.6/2.2.7 retained-cancelled-job assertions. A 2.3.0 case must
instead establish its real native state:

1. Start a genuine background shell and observe idle foreground plus the
   running job.
2. Demonstrate the production history wait timing out with the actual observed
   state and unchanged refusal semantics.
3. Stop and reap the original private producer in the local fixture. Record
   what is genuinely persisted after that boundary; do not fabricate the older
   native's cancelled row.
4. Run the unchanged production reader path and verify its safety contracts,
   real transcript/archive imports, one endpoint and source preservation.
5. Compare actual post-barrier job/scheduler state before and after the reader.
   If the old producer already drained a job, report that distinction. If the
   reader itself removes metadata or executes work, refuse support.
6. Exercise source and destination schedule refusals against native-created
   schemas, without booting a reader before a known source-schedule refusal.

These are native/process checks only. The corresponding host cases must prove
the whole-container boundary, one snapshot, immutable restart and explicit
stop-cancellation behavior with unmodified production transport.

## Current-V2 host profiles

Host fixtures must bind an explicit selected native profile to the executable
version/path/hash and their persisted metadata, reports and retries. Preserve
old profile defaults and old reports; never monkeypatch a donor constant or
report 2.3.0 as 2.2.6.

The current-V2 validation matrix includes:

- Stable history/readiness checks, including actual foreground/background
  handoffs, client drain, archive completeness and pending-session preservation.
- Separate snapshot and stop-cancellation cases with genuine version-specific
  preconditions and unchanged safety assertions.
- Global vault/Build/permission/reload/opt-out checks.
- V2-only lifecycle: first import/create, same-session stopped restart, real
  synthetic tool use, credential delivery/rotation/attach boundaries, full
  **and portable** encrypted restore, fresh identities, retained internal paths,
  exact stopped data, source preservation, reset/OS-state distinction and
  wrong-passphrase refusal before destructive lifecycle actions.

Reports must explicitly exclude legacy cross-major migration/downgrade,
interactive observations not performed, real providers/projects, actual OAuth
refresh and security/network-containment certification.

## Integration and release gates

Integrate bounded changes on a separate 1.6 candidate, retain the original
commit-count baseline and derive a new version from its final commit. Keep
legacy CI coverage and add current-native coverage; do not replace old tests
with conditional success.

Before host handoff, review the combined diff and run relevant unit/native
matrices, Python-version CI, package rebuild/installation and clean artifact
identity checks on the final source. Freeze and push the exact reviewed
candidate, then obtain external-host reports using the user's 2.3.0 executable.
No live Podman fixture runs inside oc-box, no master advancement and no
GitHub/PyPI release precedes applicable reviewed host evidence.
