# Private managed Gateway: original owner and opt-in Studio routes

`web_studio_guest_gateway._ManagedGateway` is an explicit internal composition.
It is **not** called by `oc-box web`, the owning launcher, `stop`, or
`Owner.shutdown`. There is no flag, config, route, fallback, or release in this
change. Legacy Gateway, including its one-use pairing and Studio wrapper, is
unchanged. Raw native transport remains the default for this internal class.
Forwarded authentication, Host/Origin checks and native cookies belong to
unmodified OpenCode. It reads a bounded initial head before starting native
transport; beyond the head/final response handling this is a byte relay, not
HTTP request framing, pipelining, or a half-close bridge.

## Binding and ingress

- Construct `_ManagedGateway(proof, port=0)` in the foreground with the exact
  original `GuestOwnerProof` and lifecycle lock, then call `start()` once. It
  creates the real Gateway and reserves one canonical IPv4 loopback socket.
  The one-shot owner handoff pins the same original proof, native identity and
  reserved socket **before** `listen()`. The reservation cannot be started
  through the legacy acceptor. The actual listening proof is checked before
  dispatch; a copied URL, foreign listener, named/attach connection, or second
  reservation cannot replace this scope.
- `binding` returns the exact `_ManagedBinding` while this private path is
  active. Its `owner`, `identity`, `native` and `current()` have the same shape
  as the original `_UnservedBinding`. `current()` returns only the original
  `GuestBinding` object, after checking the listener, owner run and stop
  baseline. The managed binding is also pinned to the original listener's
  recorded binding object and `AcceptedWorkers` registry; a copy or foreign
  registry cannot start a managed exec. `bind_unserved()` remains probe-only.
  This is the intended review seam for later page-scope work, not permission
  to use another registry, native connector or guest route.
- The controlled acceptor revalidates the original listener before and after
  `accept`. It owns any pending result until a proven close or transfer into
  `AcceptedWorkers`. Registration and the capacity charge precede header I/O
  and dispatch, including for silent or malformed peers. Each complete head
  can admit at most one `GuestDuplexExec` before spawning the fixed providerless
  worker over original workspace admission. It pumps OGD v3 with the native
  response-header filter and opaque duplex upgrade bytes. No legacy proxy,
  host native dial, route helper, or credential injection is used.
- The acceptor checks the same owner periodically even without traffic;
  startup and native spawn recheck the original scope. An observed loss
  fences ingress and wakes workers. This is not a continuous attestation or
  atomic with external changes between observations. There is no external
  `stop` coordination or release authority.

## Drain is evidence, not cleanup permission

Call `drain(join_timeout=...)` on the same foreground thread. It fences new
dispatch, wakes exact accepted browser sockets and admitted execs, and joins
the original acceptor and all worker threads with one shared join budget.
The acceptor closes only its original listener with an exact close check;
every accepted socket must have been closed or transferred and accounted for.
Every attempted exec must retire via its original OGD v3 `CLOSED` challenge,
strict stdout EOF and exact clean wait status. Workers which never start
native work require no native receipt. An unexpected acceptor exit, uncertain
dispatch/launch/retirement, missing receipt, or substituted descriptor cannot
be converted to success. The coordinator retains exact unresolved handles,
does not call `abandon()`, and never forces local or native termination.

Only after transport retirement, separately re-observe the exact original
owning lease with its existing bounded inspect. This includes the foreground
lock object, original service child and immutable running container
generation. The inspect is additional to the thread join budget. A lost
lease is not positive drain, even when all local sockets and threads ended.
No guest admission registry is opened in this raw-only profile.

## Private Studio fixture profile

The internal `studio=True` constructor adds a first-party document and fixed
assets and API routes to this same accepted-worker registry. It does not mint
or alter a native one-use link. On the exact native pairing request only,
the existing response-header filter redirects the native `Location: /` to
that first-party document; cookie and native request bytes are unchanged.
Native Home and upgrade traffic remain native. Non-upgrade responses forbid
browser connection reuse, rather than routing a second request on the socket.

The document challenges without granting access. Confirmation and every
bounded protected request register the exact accepted socket, then verify
the current browser cookie over counted OGD v3 anonymous/authorized probes.
Catalog, selection, conversation and Send make only their existing fixed
requests; an admitted Send is never replayed. A protected use finishes only
after its exact accepted socket closes and its relay records have retired.
Guest admission, socket/worker drain and the retained native owner proof
remain independent gates. Request/response bytes and credentials are not
stored or logged. A close or a verified queue acknowledgement is not proof
that native work finished or that it is safe to sync or stop.

There is no installed `oc-box web` caller or attach-only activation. All
current local composition tests remain distinct from host acceptance.

The loopback-only browser smoke uses real Chromium and unmodified native
2.4.1. It checks native pairing into Studio, explicit confirmation,
native catalog and transcript, one queue acknowledgement and one completed
request to a local no-tool fixture provider, same-transcript refresh,
spent-link refusal and exact relay drain before releasing the fixture
owner. Outer container inspect and exec are substituted. The native
fixture owner's clean exit is not `oc-box stop`, a managed history pass,
or an external Podman acceptance.

The private caller must keep the original handle and owner/lock scope on
`False` or interruption. A join timeout permits only another observation of
the **same** drain; there is no replacement. Do not raise a false verdict
through normal owner context unwind. Even `True` does not prove that native
application work stopped or that history is ready. This is why the private
API has no automatic cleanup/context exit and is not connected to production
ownership, stop, history sync or container lifecycle.

## Evidence boundary

The existing OGD v3 host gate was reported passed for transport source
`a70b4948b1284c1c26024f2c552cb3cf0b22189d` on no-NIC Fedora 44/rootless
Podman 5.8.1 with the fixture storage using default labels. That result does
not cover this composition. Its local tests use a synthetic original owner
registration and loopback backend with real listener, worker threads and
private subprocess pipes. They are not native/desktop/Podman or integrated
owner-shutdown acceptance. Review these exact components and separately
validate the live composition and lease retention before any route or
lifecycle activation.
