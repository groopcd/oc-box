# Full uninstall: workspace review boundary

`oc_box.workspace_uninstall.plan_workspaces()` builds a read-only review of
one selected registry and one local rootless Podman store. It is not a CLI
entry point or an executor. Launcher-only uninstall must not load this plan,
read the registry, create locks, or contact the engine.

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
  The module provides no default callback, host scan, or Podman invocation.
- **Container not verified.** Require the exact generated name, full
  lowercase 64-hex container ID, both production ownership labels, and a
  verified stopped or never-started, unmounted state. A running, paused,
  restarting, mounted, transitional, or unknown container is not a candidate.
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

## Before any future destructive release

1. Implement and independently test a host-only adapter using one pinned
   executable and audited local selector/environment for every operation.
   Never silently drop a remote/custom selector and treat a different empty
   store as the intended one. Establish callback completeness from real
   supported engine contracts, including configured/stopped and mounted
   states, reference/alias discovery, and explicit absence checks. Private
   bounded capture must not display inspect environment, raw errors or
   irrelevant labels. Reject unsupported contracts. A synthetic
   `complete=True` is not host or engine acceptance.
2. Show the exact targets and retained items before a typed confirmation.
   Revalidate under stable host and workspace lifecycle locks (using the lock
   inode current code shares), plus existing V2 control admission and
   certainty checks where applicable. Fence writers that otherwise bypass
   workspace locks, including new registration, restore, explicit stop and
   preference writes. Require operators to close active launchers, clients
   and other store users; old binaries, direct Podman users, and processes
   with already-open host handles are outside such a gate. This review does
   not acquire locks or certify quiescence.
3. Keep project files **including `.oc-box-id`** and native host history
   untouched. No final sync, purge, stop, force, or host OpenCode invocation.
   A surviving marker can require manual recovery on reinstall; an unchanged
   host archive can lose reimportability if its separate oc-box provenance
   is removed. Review that loss explicitly or retain the provenance. Any
   later filesystem executor needs independent pinned, no-mount-crossing
   verification of every removable entry; this plan does not inventory
   workspace folder contents, exports, backups or other host state.
4. Recheck identities and use non-force operations: first the verified
   immutable container ID, then its distinct reviewed home name after a
   fresh storage/usage/creation check. No label sweeps, `rm --volumes`,
   broad prune/reset, direct graphroot deletion, or removal of a replacement
   discovered during retry. Named volumes lack atomic identity-conditional
   removal in this plan. Verify postconditions, stop on a partial or unknown
   outcome, keep recovery state and launcher, fence unintended recreation,
   and require a new review. Test timeouts, refusals and partial outcomes
   offline and then on a fresh isolated external rootless-Podman host before
   making a live safety or release claim.
