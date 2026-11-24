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

The populated observation profile is now restricted to upstream Podman 5.8.1
schema, not the earlier 6.x shape. The exact accepted evidence is:

| Query | Required evidence |
| --- | --- |
| `info --format json` | Exact `version.Version: "5.8.1"`, rootless/non-remote metadata, persistent overlay and pinned canonical roots. Require `store.configFile` to name the built-in `/usr/share/containers/storage.conf` slot, independently checked absent by this no-override profile. Refuse configured volume plugins (`plugins.volume` must be exactly `["local"]`) and additional image stores. Agree on the profile and `store.containerStore.number` before and after a scan. |
| `ps --all --quiet --no-trunc`, with and without `--external` | Identical unique full lowercase 64-hex ID catalogs, with their count corroborated by `info`. Extra/missing objects or an unresolved reference refuse. |
| Volume and workspace-labeled network lists | Unique canonical resource names, one per line. Both catalogs must match a second read. Native volume list itself calls inspect on every listed volume, so plugin refusal precedes it. |
| `container inspect` | Exact catalog IDs and creation stamps, canonical names, flags/PIDs and restart policy, explicit whole Libpod `Dependencies` and supported named-volume references including subpaths, plus a no-follow pinned overlay `UpperDir`. Omitted `ConmonPid` encodes zero in this exact schema; boolean/state fields are not a mounted-state oracle. Bind source strings are only checked for lexical overlap and are never opened or returned. |
| `mount --format json` | No operands or other options. On success parse exact `id`, `Names`, `mountpoint` rows twice and join them to inspected IDs and names. Exit 125 leaves mounted state unknown, without retry or namespace fallback. It must not be interpreted as all unmounted. |
| `volume inspect` | Exact names, explicit labels, driver/scope, empty options, canonical pinned `_data` directory, nonzero `CreatedAt` and nonnegative `MountCount`. Only the 5.8.1 `omitempty` fields `Anonymous` and `StorageID` can be absent, with their documented false/empty values. Zero mounts is not proof of no stopped consumers. |
| Exact-name container/volume `exists` | Exit 0 or 1 must agree with the catalog, including absent names from the caller's review. Other codes and disagreements refuse. |

Two matching catalogs and projected inspections are not an atomic snapshot. A bound reader permits no
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
to the removal planner. In particular, lexical disjointness of a bind string
does not identify the backing object in the relevant mount namespace. The
[pinned source audit and synthetic KVM diagnostic recipe](plans/uninstall-podman-diagnostic.md)
explain remaining facts and how to measure them without printing raw replies.
This adapter exposes no mutation transport; neither it nor the actor is a
partial-release basis.

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

## Minimum production admission contract

The [exact-source guest receipts](plans/uninstall-podman-diagnostic.md#recorded-external-guest-evidence)
verify two different positive controls: `fdb5023` matched a known mounted
rootfs against the joined Podman catalog and kernel, and `3d788f9` matched a
deliberately attached same-device named-home bind against kernel mount
identity and the backing descriptor and refused the old destination pin.
Neither tested production removal. In particular, the alias run's direct
baseline remained incomplete and did not rerun the mounted-rootfs control.
The [adapter](../src/oc_box/workspace_podman.py) still exposes only an
empty-store proof to the [workspace planner](../src/oc_box/workspace_uninstall.py);
the guest helpers and their setup operations are not production admission
or a fallback for either reader.

### Close the engine, namespace, and consumer scope

- Start with one host UID, one explicitly reviewed registry, and one
  source- and distribution-audited local rootless Podman 5.8.1 profile.
  Bind the packaged executable, effective selectors and config, and held
  no-follow graph/run/volume roots for inspection and mutation. Verify
  that provisioning and review refer to this same store. Refuse remote or
  rootful engines, overrides or layouts outside the audited profile,
  plugins, additional/external stores, and missing or ambiguous bindings.
  The synthetic guest's private config and the version string prove none
  of these production bindings.
- For a populated store, require the existing, attributable rootless
  namespace for that exact UID and store. Pin a live owner/process
  generation, user and mount namespaces and their ownership/mappings;
  corroborate the original and joined store identities. In that scope,
  require a successful **operand-free** Podman mount catalog and
  corresponding kernel evidence for the exact target paths. No probe
  mounts, fresh namespace, `podman unshare` fallback, auto-start, auto-stop,
  or auto-unmount. Unavailable, partial, or contradictory evidence is
  unknown, not unmounted. Exit 125 leaves the catalog unknown, and an
  absent `MergedDir` alone supplies no verdict.
- Account for all consumers possible in the admitted scope, not just
  workspace-labeled rows. Reconcile all regular and external catalogs,
  present/absent exact names, inspected IDs and generations, and
  configured/stopped and active dependencies and mounts. Cover named
  volumes, subpaths, project and foreign binds, writable layers and any
  implicit or runtime mounts permitted by the profile; refuse unknown
  forms or an in-use target. Resolve sources in the relevant namespace
  against held no-follow descriptors and filesystem-relative mount roots.
  Check ancestors, descendants, and aliases of each writable layer,
  home, and protected path, including a different mount ID on the same
  filesystem. Neither an equal device number nor unequal path strings
  exclude a bind; zero `MountCount` does not exclude stopped consumers.
  The fixture did not discover an unknown bind, project an arbitrary
  consumer, or prove that this enumeration is complete.
- The initial supported scope needs **enforceable exclusion** of other
  namespace/store users and changes for the whole destructive window, or
  an engine contract that provides equivalent exclusion. One possible
  scope to qualify is a provisioned, exclusively managed store with an
  enforced access boundary; do not assume that exclusivity for today's
  ordinary per-user store or switch an existing registry to a different
  store. A Podman catalog, a scan of accessible `/proc` entries, and oc-box
  locks do not exclude other mount namespaces, pre-existing open
  descriptors, direct Podman/storage clients, old binaries, or a process
  that opens or renames a target after the scan. Operator confirmation
  and repeated snapshots are not that exclusion. Any unaccounted consumer
  or unbound access path blocks the whole full mode.

### Freeze authority and make interruption durable

- Build a bounded review of the registry chain and exact record bytes,
  installation owner, engine scope, protected set, and relevant inventory
  and proof generation. Require the registered expected name and labels,
  full non-ambiguous container ID, and for a local private home the exact
  name, creation generation and pinned backing. Reject stale or incomplete
  records, lookalikes, replacements and suspicious orphans; matching names
  and labels do not establish ownership of a different generation.
  Today registrations do not attest a historical store/resource identity,
  and `WorkspacePlan` equality does not retain every foreign row or the
  physical evidence behind a consumer claim. Require a verified ownership
  binding and compare the admitted proof itself, not just the target list.
- Keep review non-mutating and dry-run non-interactive. Require a separate
  typed controlling-TTY approval of the exact targets, retained data and
  destructive consequences. Under a stable host gate, hold the same
  workspace lifecycle lock inodes and V2 admission where applicable.
  Require owners and client leases to be closed, stop intent unchanged,
  and control/restore state certain; do not initialize or clear it.
  Cover writers that bypass those locks (including new registration,
  stop intent published before admission, restore and state/config writes),
  and installation changes if removing installed artifacts. Rebuild and
  compare the whole proof under these fences before dispatch and after
  each step, allowing only the verified expected removal. Any stale review
  or drift invalidates approval; never fix it up into a new target.
- The pinned native transport must issue one non-force operation for the
  exact reviewed full container ID, then its private home name, with
  independent same-store absence (original name and ID for a container;
  name for a volume) and an inventory recheck. Podman's name-based volume
  removal is **not identity-conditional**: a generation
  check followed by `rm` leaves a replacement window without exclusion.
  Keep the exclusion through transport and verification; never substitute
  purge/reset/prune, label sweeps, `rm --volumes`, graph-directory deletion,
  or a different store. A command acknowledgement alone is not proof.
  A full-mode refusal must not fall through to launcher-only removal.
- Before any request can reach the engine, persist a private, crash-durable
  pending record bound to the reviewed scope and exact resource, outside
  the entries scheduled for removal. Its publication must survive process
  loss and reboot; failure to reserve it prevents dispatch. Persist
  confirmed outcomes only after acknowledgement, independent absence,
  and revalidation. A lost reply, timeout, interruption, unsafe lock
  exit or ambiguous follow-up blocks further cleanup and recreation;
  retain launcher, registry, and recovery evidence. Future runs must
  recognize the fence, including competing lifecycle paths, and require
  explicit same-store reconciliation and a new review, not replay or
  adoption of a replacement. Rebind the live descriptors after reboot;
  namespace-local mount IDs are not durable store identities. Observation
  of absence after a lost reply does not retroactively prove who removed
  it. Store only bounded recovery metadata, never raw native replies or
  secrets. This fence does not exist in the engine actor and is not the
  V2 start-transition marker.

### Unserved recovery-fence component

[`uninstall_recovery`](../src/oc_box/uninstall_recovery.py) implements the
local write-ahead step separately from the actor and CLI. It accepts only a
non-dry-run full request and a typed, nonempty workspace review. A future
profile must independently provision one stable private recovery directory
and pristine journal for the admitted authority, outside **all** removal
targets. It must retain the expected generation and stamps independently
of the journal and recheck the directory's named, no-follow binding. It must
not capture a new baseline from what it happens to find, choose a new slot
on approval or installation changes, or recreate missing evidence. The
component does not provision, locate, or reconcile that anchor.

The component locks the verified journal without waiting. Before handing the
caller an in-process reservation for the next exact resource, it checks the
still-held authority and complete review and appends and syncs a pending row
(file, then directory). Pending rows identify the reviewed installation,
durable store, approval, live store projection, registration generation,
and exact resource. A home volume binds its creation timestamp and reviewed
backing; a container binds its full ID and reviewed writable backing. The
journal stores bounded identifiers and digests, not raw paths, registry
records, or native replies. It does not infer a durable store generation
from the live projection.

Only exact native acknowledgement, independent same-scope absence, and
revalidation allowing only the expected change can append a confirmation.
A lost reply, failed proof after reservation, or partial append leaves the
journal spent; even `NOT_DISPATCHED` is not a retry path. A fresh process
cannot resume from a journal, infer success from later absence, or unlock
admission by presenting a fresh review. Even a journal with every resource
confirmed gives no aggregate cleanup or launcher-removal authority. Changed
filesystem identities after reboot can also cause refusal; no recovery,
resumption, or automatic re-provisioning is implemented.

The journal lock only serializes users of this component. It does not close
engine access, attest the caller's digests, protect against deliberate
same-user tampering or storage rollback, or make volume removal
identity-conditional. There is no production anchor issuer or reader in
competing lifecycle paths, and no integration with the existing engine-only
actor. Offline exercises of process loss and sync failures are not a reboot
test or external-host acceptance. All production admission gates still apply.

### Keep non-uninstall authority out of the plan

Treat default and selected native OpenCode config, data, state, auth, history
and services, project trees and `.oc-box-id`, backup/restore archives and
exports, and externally selected log/backup paths as protected. Bind
exclusions without reading protected payloads or probing OpenCode. Refuse
an overlap or one that cannot be ruled out, including physical aliases.
A retained project marker may require manual recovery on reinstall;
uninstall authorizes no project repair, session mutation, final sync,
credential access, or backup/restore. The review must disclose that
deleting a private workspace home can discard unsynchronized sessions
and other workspace state.
Retain archive provenance and recovery files; an intact native archive
without its provenance is not necessarily recoverable. A separately
authorized host-settings backup does not back up a workspace home or native
host history and confers no deletion authority.

Only independently verified allowlisted oc-box-owned state and the exact
installation's owned artifacts could be retired after complete engine
verification. Never recurse over a whole state/XDG root to discover deletable
entries; unknown entries, archives and the durable fence are not targets.
The [installation ownership reader](../src/oc_box/install_ownership.py)
supplies review evidence, not a filesystem executor or package-manager
authority. Unsupported ownership stays manual; no project or native data
becomes eligible through an installation receipt.

**Concrete prerequisite:** qualify one bounded production profile with a
read-only, descriptor-backed consumer/namespace proof and demonstrate how
exclusion is maintained through a name-based deletion and its postcheck.
If uncoordinated use or replacement cannot be excluded, keep populated
admission disabled. Only then can a separately reviewed transport, lock
context, and persistent fence be integrated and tested against a fresh
isolated store. The present guest passes do not waive any of those gates.

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
   identity check; require enforceable store exclusion, not quiescence or
   launcher locks alone. Fence unintended recreation, keep recovery state and launcher
   after a partial or unknown outcome, and require a new review.
5. Test the final integrated source with fault injection, then in a fresh
   isolated external rootless-Podman store before making a live safety or
   release claim. Mock replies are not a host pass.
