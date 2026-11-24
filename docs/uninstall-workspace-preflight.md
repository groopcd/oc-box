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

## Local Podman profile and inventory stage

`workspace_podman.local_inventory()` is a read-only source-level adapter. It
selects one explicit absolute, no-link, root-owned `podman` ELF executable and
runs it through the pinned executable descriptor. It accepts a non-root host
UID, an explicit `/run/user/UID` runtime directory and standard HOME-relative
XDG locations. Container markers, remote/routing environment selectors (even
empty ones), site/user Podman configuration, storage overrides, modules,
drop-ins and saved connections are refused. A root-owned, bounded vendor
`containers.conf`, if present, must parse with the standard TOML parser and
must not select a remote connection or alternate engine directory. Python
3.10 therefore refuses this variant if that vendor file exists. This initial
profile does not accommodate many distribution or custom-store configurations;
it never silently changes their selection to a different store.

The adapter pins existing HOME/runtime, graph, run and volume directories
without reading their contents. It rechecks descriptor and path identities
and configuration boundaries around each command, retaining a single sanitized
environment and explicit `--remote=false`. The only supported scope in this
stage is a standard persistent `overlay` store: `<HOME>/.local/share/containers/storage`,
`/run/user/UID/containers`, and `<graph-root>/volumes` on the graph mount.
`podman info` can still update native runtime metadata; read-only here means
the adapter dispatches no storage or lifecycle mutation command.

The exact accepted evidence shapes are:

| Query | Required evidence |
| --- | --- |
| `info --format json` | JSON objects `host`, `store`, `version`; `host.security.rootless: true`, `host.serviceIsRemote: false`, `store.graphDriverName: "overlay"`, `store.transientStore: false`, exact `graphRoot`, `runRoot`, `volumePath`, and `version.Version` exactly `6.1.4` or `6.2.0`, with the known omission of `store.configFile`. The same profile must hold before and after a scan. |
| `ps --all --quiet --no-trunc`, with and without `--external` | Identical unique full lowercase 64-hex ID catalogs. Any observed extra external object blocks the profile. |
| Volume and workspace-labeled network lists | Unique canonical resource names, one per line. Both catalogs must match a second read. |
| `container inspect` | A JSON array bound to the exact listed IDs, with canonical `Name`, explicit `Config.Labels`, a bounded `Mounts` array, known `State.Status`, boolean `Running`/`Paused`/`Restarting`, and integer `Pid`/`ConmonPid`. This is a state observation and count of mount entries, **not** a mounted-container or dependency proof. |
| `volume inspect` | A JSON array bound to the listed names. Require explicit `Name`, `Labels`, `Driver`, `Scope`, `Anonymous`, `Options`, `StorageID`, `CreatedAt`, `Mountpoint` and `MountCount`. Only unconfigured, non-anonymous local volumes with a pinned canonical mountpoint and verified nonnegative count are observed. Omitted fields or other storage refuse; they are not defaulted to safe values. |
| Exact-name container/volume `exists` | Exit 0 or 1 must agree with the catalog, including absent names from the caller's review. Other codes and disagreements refuse. |

Two matching catalogs are not an atomic snapshot. A bound reader permits no
overlapping queries; it uses serial batches of at most eight, at most 128
entries per catalog, a 45-second scan budget and at most eight seconds per
command. Output is capped at 64 KiB for small queries and 2 MiB per inspect
batch. The subprocess adapter sends no stdin, discards stderr, caps the stdout
pipe, and reaps its own direct child on failure; errors and timeouts never
become absence. Raw inspect data stays in process memory and is neither
returned nor logged. Only selected closed metadata leaves the adapter; it
does not open project paths, native history, or volume contents.

`scan()` can return projected evidence for a populated store, but it is
explicitly incomplete. `inspect()` supplies a complete `EngineSnapshot`
only when both container catalogs and the volume catalog are empty and all
requested names independently agree. It refuses to pass populated inventories
to the removal planner. The repository has offline evidence of portions of
these shapes, **not** native acceptance of the full schema or flags, including
version-specific field omissions. No filesystem alias traversal or mounted
container/dependency proof is fabricated. This adapter exposes no mutation
transport; neither it nor the actor is a partial-release basis.

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

1. Complete and independently test the populated-store inspection projection
   and add the pinned transport for the actor's exact command adapter. Confirm
   command and absence status contracts against supported native versions.
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
