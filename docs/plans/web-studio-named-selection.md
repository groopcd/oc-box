# Named Studio selection (internal, not served)

`NamedStudioSelection` binds page selection to the **same** prepared named
services, actual browser grant registry, and original verified owner lease.
Only native 2.4.1 owners are eligible for this selection adapter. The caller
must use the existing reserved workspace TLS worker and must close the
selection registry with that site. No CLI, Studio landing route, or listener
uses the adapter yet. It does not work on a guest cookie.

The trusted TLS transport must admit only an exact `POST
/oc-box/studio/selection`, with a fixed maximum 512-byte body and no alias,
query, upgrades, or pipelining. The app rechecks the exact same-origin
browser POST and app cookie and requires one ASCII JSON object with `page`,
`sequence`, and `sessionID`. The nonce comes from the authenticated page
bootstrap, the sequence is a fresh bounded positive integer, and `null`
means native Home. The browser does not choose the lease, project, upstream
origin or credentials. A sequence does not authorize or verify a session.

Home changes only this page and does not connect upstream. An exact native
session choice registers the actual browser use before work and verifies the
existing native identity, project/location, exact session metadata, and the
absence of native location-switch history. It takes two matching snapshots
and rechecks location-switch history before strict EOF and confirmed managed
relay exit. Unsettled or incompatible metadata refuses; this is not inference
or an idle assertion. The actual origin-bound native cookie is held by the
host and sent once per fixed GET only to the same owner's private endpoint.
No browser header or native body/header is returned to the browser. Output
is only the approved registry's no-store page state.

Keep the original `SelectionRegistry` for this authenticated app lifetime so
the read and send adapters can bind an already approved exact page/generation.
The same TLS worker owns and closes the downstream socket; an interrupted
write is not retried. Unconfirmed managed cleanup fences the registry and
admission and leaves the use charged. This prevents a false owner-drain
verdict; it is not cancellation or recall of bytes already delivered.

The socket-level tests use synthetic native data and a real admission
registry. They do not establish native contract, browser, or Podman acceptance
for this composition.
