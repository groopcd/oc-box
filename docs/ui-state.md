# Native TUI selection reporting

**OpenCode 2.x integration reference.** Canonical stable native `2.x.y` releases,
including `2.3.2`, are eligible to attempt reporting; prereleases, noncanonical
versions and other major versions are not. Public SDK contracts can change
within 2.x. Eligibility is not proof of compatibility or a claim that every
release was tested. This is not a legacy 1.x client mechanism; see the
[legacy feature boundary](legacy/README.md#features-that-require-opencode-2x).

This optional V2 integration reports the route and ordered open tabs of the
container-native TUI to its existing private workspace service. It does not
launch a host-native TUI, patch OpenCode, write browser state, run tools, grant
permissions, or synchronize conversation history.

**Reports are cooperative UI metadata, not client attestation or authorization.**
Terminal focus events may be absent, delayed or synthesized. Custom client IDs
are nonsecret bookkeeping labels, not native authenticated identities.

## Reader interface

`oc_box.ui_state.read_snapshot(box, data, connection, timeout=4)` requires an
already validated, held private workspace lease. It returns an immutable
`Snapshot` with these attributes:

| Attribute | Meaning |
|---|---|
| `status` | `valid`, `missing` or `invalid` |
| `client` | Custom TUI instance ID, or `None` |
| `selected` | Exact currently routed conversation ID, or `None` |
| `tabs` | Tuple of all reported open **root** tab IDs, in native order |
| `activeRoot` | Active root tab ID, or `None` |
| `source` | `owner-tui`, `single-tui` or `unknown` |
| `reason` | Fixed category for unavailable/refused reports; otherwise `None` |

A valid home/plugin route has no selected conversation. Tabs-disabled state has
no open tab membership but may still have a selected conversation. A selected
child is **not** necessarily a member of `tabs`: bounded native GET ancestry
verification establishes its relationship to `activeRoot`. Do not replace the
exact selected child with its root or reject it solely for not being a root tab.

A consumer may normalize a valid selected snapshot into its own `verified`
selection object, retaining `selected` as `selectedSessionID` and retaining the
verified `activeRoot` and full membership separately. Missing, invalid and
no-selection snapshots must not become an automatic legacy/default selection.
Explicit user selection remains a separate consumer policy.

The reader requests only the helper's snapshot RPC and identity-matching native
session GETs in the same private service. It validates the protocol and the
owner's generation and compares a second UI snapshot after verification.
Changed route/membership/client/service epoch yields `missing/changed`. A
heartbeat's sequence increment alone does not invalidate identical UI state.
Incompatible RPC or native session responses do not produce a verified report.
There is an unavoidable interval between a verified snapshot and browser launch:
this is selection at snapshot time, not continuous cross-client focus mirroring.

## Ownership and lifetime

Normal owning preparation on an eligible native release copies a generated
package with public server, TUI and RPC exports into the private container
configuration. V1 and original host configuration are unchanged. Existing
project/managed policy may disable the helper. Unlike the Build permissions
helper, UI reporting is not reenabled by the final mandatory configuration
overlay.

After owning startup, a bounded read-only default-model catalog lookup resolves
native location/plugin readiness; it performs no inference. A ping then checks
the helper's RPC protocol version. An absent or incompatible response leaves
reporting unavailable.
The owner's in-memory UDS connection metadata then contains either:

```json
{"ui_state":{"available":false}}
```

or nonsecret `available`, `generation`, and `ownerClient` fields. Nothing is
persisted in the workspace registry or service configuration. The designated
owning TUI receives that client ID; additional TUI launches receive distinct
IDs. Only actual native TUI commands get the nonsecret process labels, through
the existing sanitized client bootstrap. The CLI plugin list is not replaced;
the independent image-preview overlay remains unchanged.

The server prefers a fresh owning-TUI report, even when blurred: opening a
browser naturally blurs its terminal. If it is gone, exactly one fresh alternate
may be used. Multiple alternates are ambiguous. Arrival/viewed times, busy
sessions, persisted tab order and history inventories are never selection
heuristics.

The public TUI SDK supplies `ui.router.current()`, reactive
`ui.tabs.enabled()/list()`, renderer focus/blur events, and `client.rpc`.
The server uses `ctx.rpc.register` and a location-local memory map. Reports carry
only bounded identity/generation/sequence, route/session/tab IDs, and a focus
tri-state; no titles, prompts, transcript parts, file paths or environment dumps.
RPC routing is pinned to the owner's internal control location, not the current
route's changing location.

Reports expire after seven seconds using the server's receipt clock. A client's
sequence high-water and release tombstone remain until location reload, so
delayed old reports cannot become fresh again after expiry. Registration history
is bounded at sixteen client IDs per location lifetime; a new ID at capacity
fails closed rather than evicting replay history. A valid existing client may
resume with a newer sequence.

A two-second heartbeat is serialized and coalesced with UI updates. Three failed
requests suspend reporting until a public reconnect/config/plugin event. Release
is best-effort; expiry remains authoritative. Reload clears the service epoch
and memory; no timers or persisted selection survive server disposal.

Already running owners do not install or refresh anything on attach. Without
availability metadata the reader returns `missing/unavailable` without issuing
native requests. A subsequent normal owning startup can enable reporting; do
not unlock, refresh credentials, force a restart, or infer current selection to
retrofit one. The web shell can use
[compatible saved membership](web-shell.md#saved-membership-fallback) without
the helper, but saved membership never establishes focus.

## Validation scope

`tests/test_ui_state.py` and `tests/ui_state_fixture.mjs` cover validation,
ordering, sequence/replay, expiry, capacity, ambiguity, cleanup and client wiring.
`tests/smoke_ui_state_v2.py` uses private synthetic state, unmodified native V2,
real TUI/server/RPC plugin loading, a forwarding-only HTTP observer and the
production stdin reader. A separate fixture-only public-SDK plugin drives native
tab selection and exit; it is not shipped or used by production.

These are native offline/process checks, not interactive desktop, actual
Konsole/Kitty focus, browser multi-tab, real-provider, rootless Podman lifecycle
or security acceptance. Showing all tabs in a web shell is a separate consumer
feature; a chooser alone is not equivalent to that requirement.
