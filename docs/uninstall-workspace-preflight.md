# Full uninstall: workspace review boundary

`oc_box.workspace_uninstall.plan_workspaces()` builds a read-only review of
one selected registry and one local rootless Podman store.
`execute_workspace_plan()` is a separate engine-only actor with mandatory
trusted lock and engine callbacks. Neither is a CLI entry point. Launcher-only
uninstall must not load this plan, read the registry, create locks, or contact
the engine.

## What blocks workspace preflight

- **Unbound state root.** Pass the raw, absolute, normalized registry path
  selected for this invocation; do not call `resolve()` first. Supply canonical
  protected paths for both default and selected native OpenCode locations,
  launcher/install locations, and other paths outside this workspace review.
  The planner compares these paths without opening them. Its registry walk
  follows no symlinks, validates ownership and modes on each directory, and
  refuses a mount at the state-root boundary or below it, including a bind
  mount with the same device number. Owned legacy non-writable-to-others modes
  are permitted. An absent root or `workspaces` directory is recorded, not
  created, and still needs verified engine evidence. No arbitrary roots or
  other state stores are searched.
- **Untrusted inventory.** Every item in `workspaces` must have a canonical
  lowercase 32-hex ID directory and a bounded, single-link, owner-controlled
  `workspace.json` with that ID, registration version 2, valid project/home
  fields and the generated `oc-box-ID`/`oc-box-home-ID` names. Unsupported
  metadata, duplicate JSON keys, partial restore state, and unknown or missing
  entries stop the entire review. The planner records directory/file identities
  and a hash of the exact registration, and repeats the whole registry scan
  after engine inspection. It does not open project directories or verify
  markers; the saved VFS baseline is checked for shape only. Stale or missing
  projects are not permission to repair a marker or create a workspace.
- **Engine scope or inventory not verified.** The trusted
  `inspect_engine(names)` callback receives the exact pairs of expected
  container/home names for explicit presence checks. It must yield a bounded
  `EngineSnapshot`, or decline completeness. Its
  scope identifies canonical, descriptor-verified graph, run and volume roots.
  A remote or rootful engine, ambiguous external storage, mount boundaries,
  overlap with the registry/protected paths/projects, malformed or duplicate
  identifiers, or missing consumer/dependency information blocks review. The
  supported layout has a separate run root and a volume root strictly under the
  graph root on the same mount. Those paths are **bindings, never targets**.
  The projection must include all containers that could use a target, including
  stopped containers, and must fail closed if external storage users, path
  aliases or expected absences cannot be checked within the supported contract.
  Registrations do not record historical Podman store identities or immutable
  resource IDs. Review binds the current verified store and objects only;
  neither labels nor an empty store prove what existed in another store.
  The module provides no default callback, host scan, or subprocess runner.
- **Container not verified.** Require the exact generated name, full
  lowercase 64-hex container ID, both production ownership labels, and a
  verified stopped or never-started, unmounted state. A running, paused,
  restarting, mounted, transitional, or unknown container is not a candidate.
  A foreign container name equal to an approved full ID is also ambiguous.
  A pod member or a container with shared dependencies, unexpected bind or
  volume mounts, or unverified writable-layer storage blocks review. The only
  supported host-backed mounts are the exact registered project bind and the
  verified private home. A full restore may have a different registered
  **container** project path. An inactive foreign consumer of the container
  or either storage target also blocks review.
- **Home not verified.** Require the exact generated volume name and workspace
  label, local driver and scope, a non-anonymous volume with default options
  and no external backing, verified creation time and storage identity, and
  canonical `<volume-root>/<name>/_data` on the same mount as the volume root.
  A count not positively verified as zero, other references by volume
  name, and observable bind aliases/subpaths or storage aliases block review.
  A missing container can coexist with a verified private home. A container
  referencing a home missing from the inventory is not proof of absence.
- **Suspicious orphans and unrelated storage.** Workspace-labeled,
  managed-labeled, or canonical generated names without an exact registration
  are not adopted. A same-name foreign resource blocks the review. Unrelated
  image/volume/container data, images loaded by restore, named networks,
  pods, extra mounts, and ordinary host packages are never targets. The
  projection may report names of workspace-labeled networks as retained.

## Engine actor and outcome contract

The caller owns confirmation and retains the original in-process `WorkspacePlan`.
Execution requires the same raw root and protected set in the same order. Only
targets present in that plan may be submitted. The required
`hold_locks(reviewed)` context continuously holds the host gate, all affected
workspace lifecycle locks and applicable V2 control admission. It must yield a
bounded checker that returns exact `True` only while the same fence is held,
including certainty and stop intent. There is no default or unlocked mode.

The actor rebuilds the entire plan under that fence before the first removal.
It issues at most one removal per original target: the full container ID
first, then the private home name. After acknowledged removal it checks
absence of both the container ID and the original name (the volume's name
only), then rebuilds the entire plan again. The only permitted difference is
the one expected disappearance; the independent inventory must also no longer
contain any removed container ID under another name. Remaining homes are thus
rechecked for generation, mounts and other consumers before dispatch. The
pass terminates on disagreement, refusal, interruption or timeout.

`podman_actions()` supplies the narrow argv mapping for `--remote=false`
`container/volume rm` and `container/volume exists`, with one explicit operand,
no force or implicit volume removal, and an injected bounded transport.
Neither it nor the actor discovers/executes Podman directly. For absence,
only Podman's exact `exists` exit 1 counts; nonzero `rm` is not proof of
non-dispatch or non-mutation. The actor checks its deadline at call boundaries;
the trusted callbacks must enforce their own time limits. No retry or fallback
engine is permitted.

- **Confirmed** means acknowledged removal, checked absence, and matching
  post-removal inventory under the verified fence at that step. It does not
  promise that outsiders cannot later recreate a name.
- **Retained** means the actor did not dispatch removal of that reviewed target;
  it does not assert current presence.
- **Unknown** means a removal may have happened but could not be confirmed.
  In particular, if revalidation is blocked after an otherwise acknowledged
  removal, the attempted target stays unknown. Earlier confirmed steps remain
  confirmed. No new target is adopted and no operation is replayed.

The actor does not modify any registry, recovery, project, or launcher file,
even on success. A fence exit failure is reported separately and prevents a
complete result. An incomplete result must keep the launcher and recovery
state. The caller must invalidate its confirmation on every attempt; this
actor has no persistent replay or recreation fence.

## Before any destructive release

1. Implement and independently test the real read-only inspection projection
   and transport for the command adapter, using one pinned host executable and
   audited local selector/environment for every operation. Confirm the command
   mapping and absence status contract against the supported native versions.
   Never silently drop a remote/custom selector and treat a different empty
   store as the intended one. Establish callback completeness from real
   supported engine contracts, including configured/stopped and mounted
   states, reference/alias discovery, and explicit absence checks. Private
   bounded capture must not display inspect environment, raw errors or
   irrelevant labels. Reject unsupported contracts. A synthetic
   `complete=True` is not host or engine acceptance.
2. Show the exact targets and retained items before a typed confirmation and
   implement the actual protected lock context: stable host and workspace
   lifecycle locks (using the lock inode current code shares), plus existing
   V2 control admission and certainty checks where applicable. Fence writers
   that otherwise bypass workspace locks, including new registration, restore,
   explicit stop and preference writes. Require operators to close active
   launchers, clients and other store users; old binaries, direct Podman users,
   and processes with already-open host handles are outside such a gate. The
   actor does not itself acquire locks or certify quiescence.
3. Keep project files **including `.oc-box-id`** and native host history
   untouched. No final sync, purge, stop, force, or host OpenCode invocation.
   A surviving marker can require manual recovery on reinstall; an unchanged
   host archive can lose reimportability if its separate oc-box provenance
   is removed. Review that loss explicitly or retain the provenance. Any
   later filesystem executor needs independent pinned, no-mount-crossing
   verification of every removable entry; this plan does not inventory
   workspace folder contents, exports, backups or other host state.
4. Bind the actor to full mode only, and preserve its incomplete/unknown
   results through CLI and state-cleanup integration. No label sweeps,
   `rm --volumes`, broad prune/reset, direct graphroot deletion, or adoption
   of a replacement. Named-volume removal is not atomic with the preceding
   identity check; require store quiescence and do not claim cross-client
   exclusion. Fence unintended recreation, keep recovery state and launcher
   after a partial or unknown outcome, and require a new review.
5. Test the final integrated source with fault injection, then in a fresh
   isolated external rootless-Podman store before making a live safety or
   release claim. Mock replies are not a host pass.
