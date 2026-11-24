# Browser workspace runtime rollout

The live `oc-box web` Studio shell includes Dashboard, Chat focus and a
metadata-only browser manager. It retains one unmodified
OpenCode iframe and the existing localhost one-use pairing. Open TUI tabs and
project history are distinct; history is an explicit bounded read of the
registered directory in the leased private service. The browser has no host
registry, global catalog, host logs, storage operations or backup authority.
It does not start a manager terminal or duplicate Textual. The initial 1.8/1.9
releases used `--workspace-layout` to select Studio explicitly; the updated
launcher opens it by default. See the
[CLI reference](../cli.md#web--browser-interface) and [shell contract](../web-shell.md).

The guest-only release does not expose host certificate preparation or
diagnostic commands. Their prototypes and tests remain in the source tree
for future development. They do not enable a TLS listener, enroll accounts
or change native pairing. A successful local certificate check is not
evidence of trust in a browser profile.

## Named sign-in is not enabled

For future host-authorized enrollment and reset, the account store can bind
an authenticated review to the existing store generation and explicit
workspace scope. A reviewed protocol upload refuses a changed store or an
expanded reset scope; it neither grants enrollment authorization nor revokes
existing browsers. The browser approval and native owner fences still have
to be implemented.

The source contains internal encrypted account records, bounded PAKE
orchestration with a pinned-descriptor worker interface, and expiring browser
admission with stream cancellation and drain hooks. The private coordinator
does not mint a native credential until a verified principal's one-use
handoff is redeemed by the matching browser. The native credential remains
server-side and is fenced to a verified owner generation and current lease.
The internal account and app dispatchers use separate browser cookies and
allow one confirmation per live bootstrap. They admit bounded protocol POSTs
and gate two explicit read-only native metadata paths and logout. The metadata
dispatcher can return a fixed status only, not an upstream response. They do
not serve native assets, arbitrary API or upgrades. No runtime listener routes
browser requests through these components. No password backend or browser
WASM is shipped. A candidate `opaque-ke` prerelease demonstrated
separate offline Chromium WASM and native interop with synthetic inputs;
that did not exercise TLS, live enrollment, cookies, origin validation or
workspace admission. See [web sign-in](../mockups/web-sign-in.md) for the
credential protection and deployment requirements.

The selected first development profile is **local named sign-in**:
`accounts.ocbox.test` and `workspace.ocbox-app.test`, both bound to IPv4
loopback and using browser-trusted HTTPS. These synthetic reserved names avoid
production DNS and deliberately separate the two sites, not just two ports.
The internal certificate preflight checks supplied material separately for each
hostname, without touching trust, name resolution or the workspace. The design
uses one loopback-only named relay per verified owner and refuses concurrent
guest access to that owner unless independent admission is proven. Guest mode
remains the default elsewhere.

The unserved launcher preflight validates both supplied certificate pairs
and binds the two fixed IPv4 loopback ports as one reservation. It does not
listen, unlock the host vault, create a workspace, or publish a link. A second
port failure releases both sockets. The socket pair does not establish
browser trust, exclusive native owner admission, or a runnable HTTPS mode.

The unserved named HTTPS transport takes ownership of one accepted connection,
selects a validated TLS context and role by the actual IPv4 loopback endpoint,
then checks that role's exact HTTP/1.1 method, route and origin. Before reading
the remaining body, it requires nonempty `application/json` for account POSTs,
exactly 54 bytes for confirmation, and no bytes for logout or metadata. It
closes after a read-only probe or, in the fixed-response exchange, after using
one explicit role handler to produce one bounded, fixed-length JSON envelope.
The response writer cannot set cookies, redirect, upgrade or relay a stream.
The transport refuses surplus it sees while framing or during one immediate
probe; it cannot detect every byte still in flight and never parses or forwards
a second request. It does not use EOF to delimit the request.

The TLS handshake, headers (16 KiB, 64 fields), body and response (8 KiB each)
share a five-second I/O deadline. A trusted synchronous dispatch callback
receives that deadline and must honor it. A late result cannot start a response,
but this does not preempt the callback, roll back its effects or verify its
cleanup. There is no production dispatcher, listener or handoff receiver.

The separate unserved authenticated native relay is not the fixed status
dispatcher or guest Gateway. It accepts only `GET /api/info` and
`GET /api/session/active`, with no body, query or alias. It requires the exact
workspace Origin even on GET, plus the app cookie, a bound browser grant and
the current private owner. It registers cancellation before credential
resolution or connecting, reconstructs the native Host, Origin and private
Cookie, and keeps the request registered through response writes and socket
cleanup. Refusals close without sending a native error body or falling back to
guest admission. Revocation interrupts the tracked sockets. It is not atomic
with a write already in progress, cannot recall sent bytes, and does not prove
that native work stopped.

Only complete, fixed-length JSON replies of at most 8 KiB are eligible (8 KiB
header limit, one response followed by EOF). The narrow 2.3.2 schemas permit
the version, positive PID and pinned private loopback URL, or a map of native
session IDs with `running` state only. Unexpected fields or values refuse. The
body bytes are preserved, but all browser response headers are generated
locally: no native `Set-Cookie`, challenge or redirect is forwarded. The
exchange uses the earlier of the TLS I/O deadline or the session's absolute
expiry. It has no default connector; the private endpoint's container loopback
port must never be treated as a host loopback destination.

Synthetic HTTPS and private socket tests cover matching handoff, byte-exact
replies, route/lease refusals, malformed native replies, and cancellation
before registration, during startup, during native reads and before browser
write. They do not establish native wire/schema compatibility on a real owner,
browser deployment, or live cleanup. Before serving anything, a future
integration must verify the owner-bound bridge, exact native replies, binds,
concurrency, owner/expiry sweeps, browser compatibility, and drain before lease
release. There is no native asset or arbitrary API routing, SSE, WebSocket,
listener, CLI or installation integration in this slice.

### Private owner lease boundary

The unserved [`web_named_owner.py`](../../src/oc_box/web_named_owner.py)
consumer binds sign-in, `NativeCookie` and the metadata relay to the same
explicit `VerifiedOwnerLease`. The handle is an internal trust contract, not
proof that its producer verified anything. It pins the workspace ID, a
per-owner generation, the full immutable container ID, the native version
(exactly **2.3.2**) and the validated private `127.0.0.1` endpoint. A
read-only, bounded observation must still confirm that exact owner and
container run. The consumer also polls the original held same-user Unix attach
stream without reading its contents: EOF, error, unexpected data or loss of
the descriptor fences the binding permanently. A connected socket by itself
does not attest native readiness. Pairing targets the full container ID; the
only secret it passes to the existing isolated worker is the native service's
ephemeral control credential. No account password or vault value is passed
through this boundary. The cookie goes upstream only. No credential refresh,
replacement attach, host OpenCode, host-loopback upstream dial or guest
fallback is attempted.

The runtime remains unchanged:

- [`workspace_service.Owner`](../../src/oc_box/workspace_service.py) starts its
  private native service via the container `opencode_v2_io.py lease` worker.
  `Owner.handle()` checks same-user Unix peer credentials, workspace ID,
  readiness and the close fence. It counts `attach` clients until that stream
  closes or the owner ends; `wait_clients()` uses that count before shutdown.
- `workspace_service.attach()` checks the registered project/directory and
  the owned running container mount, then uses the private runtime
  `socket_path()` and verifies the peer and endpoint. The original connected
  `stream` inside `attach()` is the retained control handle; the yielded
  connection dictionary is not that handle. `controller_running()` only
  probes the path and does not attest a ready owner or retain a lease.
- `inspect_owned()` can return the full Podman `Id`, but the current owner
  reply does not bind that immutable ID and running instance to the stream.
  An optional UI-reporting generation is not a mandatory owner generation.
  The existing guest `relay_command()` opens an exec pipe, not the registered
  cancellable socket required by the named connector.

Before enabling named mode, a reviewed runtime producer must bind the native
owner's exact generation and running container identity to the held stream,
including stop/close and native liveness checks. It must reserve one named
lifetime per owner, avoid rewrapping a failed lease, supply a confined and
cancellable connector to that exact container, and prove that guest admission
cannot bypass named policy on that owner. The listener must stop admission,
run owner/expiry sweeps, cancel and drain workers, then release the attach.
Checks are not atomic with native execution and cannot recall already
transmitted bytes. Synthetic owner observations and Unix socket pairs check
the consumer contract only; they do not establish this producer, connector,
full Studio capability, or live cleanup. Public one-use link behavior remains
unchanged.

A later LAN instance can use distinct existing sites and certificates under
operator control. Isolated guest LAN tests can establish transport behavior
using a synthetic private CA, without touching public DNS or requesting a
public certificate. That does not establish public trust or DNS-01 issuance
against an operator's production domain.

Before turning on experimental named sign-in, independently review the exact
PAKE build and applicable distribution terms, choose the certificate/hostname
and trust model, then test host-authorized enrollment, single-use browser-bound
handoff, native cookie security, client-scoped revocation and active stream
cleanup on the chosen browser and Podman target. No HTTP or plaintext-password
fallback. Only after the loopback gate passes should a separate explicit LAN
bind be considered. Host-wide manager features require their own authorization
design, not merely a stronger workspace login.

The [upstream stable `opaque-ke` 4.0.1](https://crates.io/crates/opaque-ke/4.0.1)
lockfile includes a dependency affected by
[RUSTSEC-2026-0387](https://rustsec.org/advisories/RUSTSEC-2026-0387.html).
The lab used `5.0.0-pre.0`, which resolves a patched version of that dependency.
This does not approve the exact native or browser artifacts, assess the
exploitable surface of this profile, or settle distribution and
[IPR review](https://datatracker.ietf.org/ipr/6863/). An external verifier alone
would not close the gate: the browser still needs an independently reviewed,
matching client served by the trusted account origin. Neither half is fetched
or distributed by oc-box.

### Source-only browser transport

`src/oc_box/web_pake_client.mjs` is an offline-tested adapter, not an installed
or served browser asset. It has no page, password field, enrollment, WASM
loader, handoff POST or runtime caller. It binds to the configured local
account and workspace origins, verifies the public transcript context and
supplies the separate client and verifier IDs. It sends only the username
and KE1 on `/start`, then the challenge ID and KE3 on `/finish`, both as
bounded, same-origin HTTPS JSON POSTs without redirects or fallback.
The module allows one controller per account document and only one live
exchange or pending ticket at a time. Cancellation aborts the transport and
blocks progress at checkpoints; it cannot interrupt synchronous WASM or undo
a POST the ingress has already processed.

A successful response yields only an opaque in-memory ticket carrier. It
expires locally after at most five seconds, never later than the attempt
deadline, and is invalidated on `pagehide`, cancellation, explicit `discard()`,
or a single `take()`. A future caller may take it only for the separately
reviewed POST handoff, never a URL. Taking it does not prove app admission;
the server remains authoritative for expiry, browser binding and replay.

The offline binding now supplies `openAttempt` in the trusted controller
configuration. Before any password access, `openAttempt()` must return a fresh,
empty handle with synchronous `start(readPassword)`, `finish(bindings)` and
`discard()`. `exchange({username, readPassword, signal})` makes a one-shot byte
reader available only to `start`, after taking ownership of the handle.
The trusted binding must clean up those local bytes, verify the server proof
before emitting KE3, and confirm teardown by returning `true` from `discard()`.
Uncertain teardown permanently fences that controller; a new one cannot
replace it in the same document. The earlier one-step `createSession`
contract is not accepted. The wire, `finish` binding parameters and artifact
review requirements are unchanged.

The adapter does not validate artifact provenance or establish memory erasure.
The Node fixture checks both origins and the exact Python wire format with a
synthetic session that checks its arguments; it does not verify a password,
make browser requests, or establish browser trust. Run it from a source checkout:
`PYTHONPATH=src python3 -B -m unittest discover -s tests -p test_web_pake_client.py -v`.
The separate checkout-only [Chromium/WASM and verifier check](../../tools/web-pake-research/README.md#browser-conformance-scope)
exercises the same contract with pinned artifacts through a private CDP pipe.
It is not a trusted-HTTPS or host admission test.
Activation still requires independent review of the exact native/WASM
artifacts and interface, matching host-authorized enrollment (including
password input encoding), a trusted account page, and the live gates above.

## Isolated feasibility results

- **Local HTTPS:** Two fresh, sandboxed Chromium 159 lab runs each passed
  30 checks with separate synthetic `.test` sites and a private trust profile.
  The browser performed enrollment and login through real OPAQUE WASM and a
  separate native verifier; it sent protocol messages, not the password.
  Untrusted and wrong-name certificates, a wrong password, an altered
  final proof, a different browser session, and handoff replay were refused.
  The successful handoff used a top-level POST and same-site confirmation,
  then redirected to a clean app URL. The lab app was synthetic: this does
  not validate native workspace pairing, durable enrollment, stream
  revocation, or Firefox/Brave.
- **Private LAN:** A disposable Fedora KVM guest passed 14 transport and
  certificate checks with two isolated guest network namespaces and an
  ephemeral private CA. Loopback refused the peer; an explicit guest LAN bind
  admitted it. Chain/name, exact Host and exact Origin checks were exercised.
  No port was forwarded to the real host, no public CA was contacted, and
  this was not an oc-box named LAN listener or an ACME test.
- **Private native pairing:** On exact `ab0b9a0`, a disposable Fedora 44 KVM
  guest with rootless Podman 5.8.1 and unmodified OpenCode 2.3.2 held a real
  attach lease and exercised server-side pairing. The cookie authenticated
  without Basic auth; foreign origins, missing/invalid credentials, owner/
  scope mismatches and use after lease close refused. This was the private
  adapter, not named browser authentication or native cookie revocation.
  The guest's offline Chromium library check was incomplete, so the guest
  did not exercise the HTTPS browser composition.
- **Browser and native streams:** Two private Chromium HTTPS runs on exact
  `2767d46` passed all 12 checks with real OPAQUE and OpenCode 2.3.2. Copied
  tickets and replay refused, and the native cookie stayed on the server.
  Logging out one browser closed only its native event stream and rejected
  its stale browser cookie; the other stream received a fresh keepalive
  before its own logout. Native pairing can return identical cookies, so
  revocation was enforced by the host-side browser admission, not by
  invalidating a native cookie. The HTTPS dispatcher and owner lease were
  fixtures rather than a public listener or real Podman attach. This did
  not exercise native WebSockets.

Option 3 is still the default: the native one-use link on localhost. Neither
lab enables a production named or network mode.

## Final integration and publication checklist

The development candidate through `2767d46` includes the quiet preparation
logs and terminal brand mark. Keep those changes and the default web/native
TUI behavior intact when integrating the feature. Do not infer readiness
from a green check on an earlier branch commit.

1. Review and test a separate named HTTPS ingress. It must admit every HTTP
   request and upgrade before opening private upstream work, with no public
   native pairing route or fallback to guest admission. Preserve the pinned
   native cookie check as a separate requirement. A browser must never
   receive the server-side native cookie as part of named sign-in.
2. Review the exact PAKE artifacts and distribution terms before adding any
   shipped backend. The pinned prerelease was reproducible in the isolated
   Linux/native and Chromium/WASM labs; build reproducibility and protocol
   interop are not a security or licensing review.
3. On an exact candidate, verify enrolled named accounts, same-browser
   handoff, native workspace binding, and per-browser logout with active
   HTTP streams and WebSockets. Confirm draining and account/owner
   revocation, not just clearing a cookie. Audit password and native-code
   handling across diagnostics, proxy paths and package assets.
4. Review the network bind separately. Private LAN TLS feasibility does not
   permit enabling a LAN listener, touching production DNS, or requesting a
   certificate. The host-wide manager also needs separate authorization.
5. After the intended development tip is frozen, run the
   [build-only release preflight](../release-security.md). Passing that
   pipeline neither enables named sign-in nor authorizes an exact-source
   tag, matched distributions, or publication.
