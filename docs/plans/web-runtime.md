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
existing browsers. Production native owner exclusion/revocation is still
required.

The in-process approval gate reserves one live account bootstrap for one
reviewed workspace registration and uses the same local PAKE identifiers.
The [staged coordinator](web-enrollment-stage.md) validates the canonical
upload with OCOR and verifies possession with a separate OCOP exchange
against the exact tentative setup and record. It issues no sign-in handoff.
Only the one-time host result carries its live guard into the account
store's locked compare-and-swap. Expiry and retirement invalidate the
guard; a possibly active verifier prevents verified drain. Offline
composition with the real helpers opens no listener and writes no store.
The unserved foreground commit can publish after local confirmation only
with an independently held owner fence. There is no production producer for
that fence, default authority, served browser enrollment, or launcher route.

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

The unserved bootstrap ingress now admits the exact top-level workspace
entry and an account-site document POST containing only its single-use
ticket. It uses separate Secure, HttpOnly, SameSite=Strict bootstrap
cookies, checks current owner binding before and after the operation, and
never includes a ticket in a URL or accepts a password field. The account
step yields only a bootstrap cookie; the reviewed login assets, response
writer and live routing still need to be connected.

The prelogin-only transport reuses the already validated TLS contexts
and admits only these two exact documents and their fixed body shapes.
It checks the owner through response delivery. It is not yet a dispatcher
for login assets, handoff, Studio, or native API traffic.

`tests/smoke_named_bootstrap_browser.py` exercises these handlers and the
bounded TLS transport with Chromium on two synthetic loopback HTTPS sites.
A fresh browser home trusts
only the test CA added to its private NSS database for this fixture. The test
verifies a real cross-site form submission, host-only bootstrap cookies, and
replay refusal without certificate bypass or changes to the operator's trust.
It does not establish durable enrollment, named workspace admission, or
external-host acceptance.

The internal named dispatcher now routes the two entry documents, account
proof, browser handoff, same-origin confirmation, logout, and the bounded
native metadata requests through one validated TLS request. Confirmation
registers delivery under the newly issued browser admission and returns an
empty 204 with only the opaque app cookie. Failed delivery revokes that
admission; logout acknowledges only checked drain. The internal listener
uses this same dispatcher after TLS, without a second handshake or a transfer
of TLS-socket ownership to the upstream relay. Stopping the sites cancels
registered upstream traffic before joining workers. A socket-level
integration fixture covers both verified loopback listeners and the complete
request path through logout, using a synthetic verifier, owner and upstream.
The internal dispatcher may serve the exact reviewed login bundle and, only
with explicit Studio composition on the verified 2.4.1 profile, the pinned
iframe-free Studio document, recent catalog and scoped conversation/Send
routes. It is not yet a launcher entry point.

The unserved named HTTPS transport takes ownership of one accepted connection,
selects a validated TLS context and role by the actual IPv4 loopback endpoint,
then checks that role's exact HTTP/1.1 method, route and origin. Before reading
the remaining body, it requires nonempty `application/json` for account POSTs,
exactly 54 bytes for confirmation, and no bytes for logout or metadata. It
closes after a read-only probe or, in the fixed-response exchange, after using
one explicit role handler to produce one bounded, fixed-length JSON envelope.
The general response writer cannot forward upstream cookies, redirects,
upgrades or streams. Only fixed named confirmation and logout code can
issue or clear the opaque app cookie.
The transport refuses surplus it sees while framing or during one immediate
probe; it cannot detect every byte still in flight and never parses or forwards
a second request. It does not use EOF to delimit the request.

The TLS handshake, headers (16 KiB, 64 fields), body and response (8 KiB each)
share a five-second I/O deadline. A trusted synchronous dispatch callback
receives that deadline and must honor it. A late result cannot start a response,
but this does not preempt the callback, roll back its effects or verify its
cleanup. This writer has no production dispatcher, listener or browser
document handoff route.

The separate unserved authenticated native relay is not the fixed status
dispatcher or guest Gateway. It accepts only `GET /api/info` and
`GET /api/session/active`, with no body, query or alias. It requires the exact
workspace Origin even on GET, plus the app cookie, a bound browser grant and
the current private owner. It registers cancellation before credential
resolution or connecting, reconstructs the native Host, Origin and private
Cookie, and keeps the request registered through response writes and socket
and process cleanup. Refusals close without sending a native error body or
falling back to guest admission. Revocation interrupts the tracked sockets. It
is not atomic with a write already in progress, cannot recall sent bytes, and
does not prove that native work stopped.

Only complete, fixed-length JSON replies of at most 8 KiB are eligible (8 KiB
header limit, one response followed by EOF). The two exact native profiles
are **2.3.2** and **2.4.1**, which use the same audited wire format here.
`/api/info` must match the leased version and endpoint and contain the
expected native hostname, temporary-path and capability fields. The relay
discards those private fields and emits only the version, positive PID and
pinned private loopback URL. `/api/session/active` preserves validated body
bytes for the closed map of native session IDs with `running` state only.
All browser response headers are generated locally: no native `Set-Cookie`,
challenge or redirect is forwarded. The exchange uses the earlier of the TLS
I/O deadline or the session's absolute
expiry. It has no default connector; the private endpoint's container loopback
port must never be treated as a host loopback destination.

Synthetic HTTPS and private socket tests cover matching handoff, metadata
projection, route/lease refusals, malformed native replies, and cancellation
before registration, during startup, during native reads and before browser
write. Subprocess fixtures replace only the Podman exec invocation: they
exercise the same registered socket and actual child wait, including valid
reply EOF before child exit, cancelled spawn, deadline expiry, nonzero exit,
and forced host-child cleanup. They do not establish Podman stdin/EOF or remote
worker cleanup behavior, native wire/schema compatibility on a real owner, or
browser deployment. There is no native asset or arbitrary API routing, SSE,
WebSocket, listener, CLI or installation integration in this slice.

### Private owner lease boundary

The unserved [`web_named_owner.py`](../../src/oc_box/web_named_owner.py)
consumer binds sign-in, `NativeCookie` and the metadata relay to the same
explicit `VerifiedOwnerLease`. The handle is an internal trust contract, not
proof that its producer verified anything. It pins the workspace ID, a
per-owner generation, the full immutable container ID, the selected exact
native version and the validated private `127.0.0.1` endpoint. A
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

The unserved [`web_named_protocol.py`](../../src/oc_box/web_named_protocol.py)
producer is an explicit, in-process owning-launcher opt-in. It requires the
actual held workspace lifecycle lock and an unchanged stop-intent baseline
from normal owning preparation. It will not convert a live default owner or
replace a controller socket, even one that looks stale. Before starting its
private native worker, it inspects the workspace container's ownership label,
registered mounts and isolated network, capturing a full immutable ID and
running `StartedAt` value. The worker starts against that full ID, checks the
actual authenticated `/api/info` response against the expected **2.3.2** or
**2.4.1**, and reports readiness only over the existing private pipe. The host
rechecks the exact run before publishing or lending the owner. The pairing
worker, cookie scope and relay require that same release, not merely any
supported one; alternate releases, aliases and untested patches do not match.

The private `oc-box.named-owner/1` request and reply use same-user Unix peer
credentials and a fresh challenge authenticated by a per-owner in-memory
capability. The reply binds that challenge, an owner generation independent
of UI reports, the workspace, version, endpoint, container ID and `StartedAt`
to the original `_attach_control()` stream. Neither process IDs nor the
client count are evidence of native identity. The reply contains only lease
metadata; the same-process borrower uses the owner's existing private control
credential after verification rather than transmitting it over this socket.
The holder rechecks the original readiness worker, running instance and
stop/closure fence; it will not reconnect or follow a replaced container by
name. This is not a guarantee of
continuous native HTTP responsiveness or an atomic transaction with native
execution. The private control credential never goes to the browser.

Mode is fixed before startup. A legacy owner will not accept the named
action, and a named-only owner rejects **both** legacy `info` and `attach`
requests. Legacy web, TUI and terminal requests cannot be distinguished on
that wire, so none receive native credentials from an opted-in owner. The
owner-local legacy `web_session()` path also refuses. The default `Owner`,
ordinary `attach()` and public one-use link behavior are unchanged.

The verified producer can explicitly bind
`web_named_owner.connect_workspace_native` to the launcher module in its lease.
The connector uses the existing `workspace_service.relay_command` worker
against the full immutable container ID and the exact leased container
loopback port, never the container name or host loopback. Only the maintenance
host environment and cleared container environment are used; the native
cookie travels in the authenticated HTTP request, not in argv or environment.
The named worker starts from `/` with isolated Python and site hooks disabled.
The ordinary one-use Gateway is unchanged.

One admitted request reserves its Unix socketpair and child record before
launch. Admission, owner and monotonic deadline are checked before and after
the synchronous spawn: cancellation racing spawn may still start the reserved
child, but the relay will not then send HTTP. Complete response framing is not
enough to publish metadata: the managed relay requires the exact child to be
reaped with a natural zero exit before writing to TLS. Closing the socket
allows a bounded EOF grace. A fallback can signal only that exact host child
via a Linux PID descriptor, never a process group. No other code may reap
this child. A forced or nonzero exit, a spawn without a returned handle,
or an unconfirmed wait does not prove that the container relay exited. The
named lifetime fences, retains the use rather than releasing capacity or
claiming drain, and does not retry. After fencing admission for shutdown the
lease holder must require `relay.wait_drained()` before releasing the attach,
including uses from previously revoked grants. A false result does not
authorize replacement, fallback to guest mode, or automatic release.

Before serving, compose the listener and browser flow, run owner/expiry
sweeps, and drain all workers before releasing the attach. Verify old-relay
drain and guest exclusion across live owner transitions, actual Podman
cancellation and natural-exit semantics, exact native replies, binds,
concurrency, and browser behavior on the intended external host. These
checks cannot recall already transmitted bytes. Synthetic control-socket
and subprocess fixtures are not Podman, native-wire, browser, or full
Studio acceptance.

The internal service assembler now binds the entry, account, app, and
metadata relay components to the same reserved profile, verified attach,
and locally authenticated account-store snapshot. A change to that store's
reviewed generation permanently fences the binding. Service teardown
checks verifier and relay drain but does not close the borrowed control
stream; the lease holder must keep it until listener workers are also
confirmed stopped. The assembler performs no vault prompt, listener
activation, or workspace transition.

`web_named_foreground.NamedForeground` is the internal launcher handoff for
an already running, opted-in named owner. Keep its original reservation,
reviewed asset context and `AccountSnapshot` through `start(expires_at=...)`.
It attaches through the actual owner-control protocol, selects the managed
relay for that exact container, and returns only the fixed workspace entry URL.
Retain the foreground handle on startup refusal. Only a true `close()` permits
releasing the borrowed account snapshot, reservation and owner. A false result
leaves the attach held; it is not permission to sync, stop or restart as if the
client had exited. No CLI, artifact distribution or host unlock is enabled by
this composition. A local fixture uses real TLS, the signed owner-control
exchange and encrypted-store replacement with synthetic native/Podman I/O.

`prepare_named_launch` is the internal pre-owner entry point for explicit
host choices. It reserves both loopback sockets, checks the reviewed browser
and verifier source/binary inputs, and then prompts on the host for the
separate encrypted **web account store** passphrase. It does not initialize an
account store, ask for a browser password or provider vault, select compiled
artifacts, or inspect/create/launch a workspace. The caller still owns
normal vault and workspace preparation. Its retained handle starts only one
exact owner's sites with a scope present in the authenticated account store.
The host can require that scope before any provider-vault or workspace
preparation; launch checks it again before attempting the owner attachment.
Once an owner attach exists, a false close retains every path-bound resource;
do not treat that as a normal client exit. No CLI calls this internal API yet.

`load_named_inputs` accepts one explicit, owner-only regular JSON profile
(`0400` or `0600`, at most 16 KiB, no symlink or additional hard link).
Its exact v1 fields are `version: 1`, `tls` (the four absolute `account_cert`,
`account_key`, `workspace_cert`, `workspace_key` paths), `wasm` and `verifier`
(each `{path, size, sha256}` for an independently reviewed artifact), and
the absolute `account_store` path. Unknown fields, including password or
secret fields, refuse. The profile read does not open the referenced files,
choose artifacts, update profile files or authorize distribution.

`run_named_client` is the foreground loop for a prepared named Studio
client. It opens only the fixed, token-free workspace entry in a sanitized
browser subprocess, or prints it for explicit opening. A caller must handle
`NamedClientUndrained` with the original handle and owner held: routing this
through ordinary cleanup that releases the owner would be unsafe. An interrupt
or error during `close()` is also an unconfirmed drain, even if a prior
interrupt already ended the client loop. The launcher integration and its real
host cancellation tests remain gated.

`run_retained_named_client` is the foreground seam for a future owning caller
that holds both the workspace lifecycle lock and a durable named stop fence.
It latches Ctrl-C instead of unwinding those scopes. If teardown was not
confirmed, it retains the original owner/handle and checks only the existing
drain; it cannot start again, declare success, or release the caller to history
and container stop without a positive verdict. This does not route
second-terminal `stop` or make a process exit a drain acknowledgement.

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

### Unserved browser handoff

The trusted account page may take the adapter's opaque ticket only to submit
one `ticket` field in a top-level `POST /oc-box/handoff` to the configured
workspace origin. Its CSP permits only that form destination; the credential
controls have no form names and their native form action is blocked. The
transfer helper clears and removes its submission form. No grant, credential
or return URL is taken from a query or a workspace-supplied setting.

[`NamedHandoffIngress`](../../src/oc_box/web_named_handoff.py) is an unserved
helper for the workspace side. The transport must first select the exact TLS
role from the listener and SNI and bound the whole request. The receiver
requires the opposite account Origin, `cross-site` / `navigate` / `document`,
no Cookie header, and exactly 61 bytes of form data: `ticket=` and one canonical
54-byte token. It checks that the ticket is still pending; it does not claim
the bootstrap or mint a credential. The no-store document carries the token
only in its nonce-bound script, which removes itself and retains the token in
a private closure. The page drops its reference on submission, page exit, or
a local 30-second timer; only the server decides expiry. It has no third-party
content, script asset, fallback form, or token in a URL, DOM field, cookie or
storage. The POST body and protected
response still carry an opaque ticket; this is not browser-memory or history
erasure, and the transport must not log either body.

An explicit click sends a non-redirecting same-origin `POST /oc-box/confirm`
with exactly the 54 ticket bytes as `text/plain`. The helper requires workspace
Origin and `same-origin` / `same-origin` / `empty` Fetch Metadata and delegates
to `NamedAppIngress.confirm`. That method alone matches and consumes the ticket
and claims the actual HttpOnly workspace bootstrap before native pairing. The
wrapper returns an empty `204` with only the opaque `__Host-ocbox-app` cookie,
not the native credential. The page then replaces its URL with `/`; the named
server must independently gate that landing and every app request, with no
guest fallback. Expiry/replay or a different browser cannot redeem a ticket
even if a document was already issued. A failure is not proof that a
pairing already in progress was undone.

### Account assets and deployment contract

[`web_named_account_assets.py`](../../src/oc_box/web_named_account_assets.py)
projects the bounded account card from the approved [Studio sign-in markup
at `21e966c`](https://github.com/groopcd/oc-box/blob/21e966c/docs/mockups/web-manager.html).
It keeps the masthead, card, credential layout, Show/Hide control and footer,
without demo users, preview controls, a guest link or a vault prompt. The
workspace label is bounded, escaped display text from trusted host configuration;
it never selects an origin, workspace or credential. Both origins come from
the trusted `LocalOrigins` profile, not a request, query parameter or redirect.

- **Prepare before serving.** A trusted host caller supplies `origins`, absolute
  paths `wasm_file` and `verifier_file`, and a separate `ReviewedArtifactPin`
  (`size`, raw 32-byte `sha256`) for each to `prepare_account_assets(...)`.
  The pins must come from an independent review of a matching OCOP build, not
  from the paths, a request or a manifest next to the files. There are no
  compiled default pins. Stage only independently approved local bytes as
  regular, single-link files owned by the operator or root, without writable
  group/other bits, special permissions, or symlinks in any path component.
  Direct Cargo build outputs can be hardlinked; those paths are refused.
- **Keep the verified snapshots.** Source HTML/CSS/JS have fixed sizes and
  digests for this source revision; the loader also checks file identity,
  size and SHA-256 for both compiled inputs. It holds the WASM as immutable
  bytes (at most 1 MiB) and copies the verified ELF into a private, close-on-exec
  executable memfd with all required seals. Linux `/proc`, no-follow file pinning
  and existing worker fd-exec/pidfd support are prerequisites; capability
  refusals have no filesystem or subprocess fallback. The context yields
  `bundle`, `verifier_fd` and `verifier_sha256`. Prepare the worker/service
  that duplicates the descriptor inside the context. Never send or serve the
  ELF, choose a path with request data, or reopen a verified source for traffic.
- **Project only fixed responses.** Call `bundle.document(entry)` only with the
  verified `AccountEntry` from the exact bootstrap POST. It emits the unchanged
  bootstrap cookie and the isolated no-store document. `ACCOUNT_ASSET_PATHS`
  is the exact six-path allowlist, not a directory mapping. Call `bundle.asset`
  with the bounded raw request and a trusted `translate_nonce` that checks the
  same owner's current account bootstrap. It admits only a matching same-origin
  subresource GET with the right Fetch Metadata, not download/navigation,
  partial, conditional, alias or query requests. `web_named_transport`/serving
  still owns TLS/SNI and role, absolute deadlines, length framing, owner rechecks
  before writes, worker drain and the distinct workspace/guest routes. Send the
  exact lengths and types without transforming, compressing or redirecting the
  assets; do not log opaque request/response data.
- **Keep input local.** The account CSP permits only the fixed scripts and
  stylesheet, the WASM/PAKE requests and the exact cross-site handoff form.
  Chromium verifies the bounded WASM length and SHA-256 again before enabling
  controls. Credentials have no form names; the credential form has no submit
  button or action. Only a trusted user click or Enter starts the local reader.
  It rejects malformed Unicode and inputs that do not fit in 1,024 UTF-8 bytes
  without trimming or normalization, and clears/remasks the field on submission
  and page exit. These checks do not protect compromised account code, browser
  memory, or a malicious controlling service worker; deployment must keep the
  account origin dedicated and free of service workers or unrelated content.

Both installation formats carry the exact source HTML, CSS and JavaScript for
this bundle. This is not a compiled artifact decision: no WASM or verifier
binary is included or selected by the installer. The account source hashes
and limits are unchanged for installed copies.

The internal listener accepts `start_named_http(..., account_assets=prepared)`
only for the same HTTPS profile and the digest used to prepare its verifier.
It rechecks the original live account bootstrap before sending a subresource;
it never serves the ELF, a file selected by the request, or an account asset
on the workspace origin. No launcher entry point or package selects, downloads,
or bundles compiled artifacts. Distribution requires independent security,
provenance, license and applicable IPR review of the exact paired binaries,
followed by separate activation, owner/relay, host-authorized enrollment and
publication checks. The review pins and synthetic conformance are not approval.

### Synthetic two-site browser composition

[`smoke_named_pake_browser.py`](../../tests/smoke_named_pake_browser.py) composes
the exact bootstrap handlers, `AccountIngress`, `LocalNamedSignIn`, and the
Python worker with the pinned Rust verifier and real browser WASM. It now serves
the unserved account bundle above using a synthetic workspace label. The positive
path is not intercepted. A separate negative case substitutes a same-size,
still-compilable WASM module and checks that the account stays disabled. A
script-triggered submit, click or key event also cannot start PAKE or serialize
the populated credential controls. Trusted mouse and keyboard paths and the
approved Show/Hide card are exercised.

The fixture starts two disposable IPv4 loopback HTTPS endpoints in a private
network namespace. A fresh untrusted Chromium profile refuses both sites;
another profile trusts the synthetic CA only in its temporary NSS database.
The real cross-site bootstrap POST sets separate host-only, HttpOnly, Secure,
`SameSite=Strict` cookies. A second browser context cannot replay that entry
ticket. An unpaired browser cannot load the WASM asset. A wrong password sends
KE1 but no KE3, and a matching password sends only the declared PAKE fields.
The earlier `8867358` run stopped at an in-process ticket check. The expanded
smoke issues real tickets and exercises both POSTs in Chromium. It refuses a
wrong transfer origin, a copied ticket confirmed by another bootstrapped
browser, replay at both endpoints, and expiry (a fixture-only advance of the
server handoff clock). Only the matching browser lands on a static admitted
marker. The native pairing worker really mints and verifies one cookie with
unmodified OpenCode 2.3.2 on private loopback. The tested browser receives only
the separate opaque app cookie, and an independent request confirms the
retained native cookie still authenticates that same private service.

Only a synthetic recorded account is used, seeded in memory by the pinned
fixture. The browser, WASM, verifier, seed and OpenCode 2.3.2 binaries are
checked against the existing SHA-256 pins; no artifacts are downloaded,
rebuilt or installed.
Run with already-available Playwright, OpenSSL and certutil (NSS tools). Example
on a Linux host with user/network namespaces and `ip`:

```sh
unshare --user --map-root-user --net sh -c '
  ip link set lo up || exit
  fixture_home=$1; shift
  exec unshare --user --map-user=1000 --map-group=1000 \
    env -i HOME="$fixture_home" PATH=/usr/bin:/bin PYTHONPATH=src "$@"
' sh /path/to/empty-private-test-home /path/to/offline-playwright-python \
  -B tests/smoke_named_pake_browser.py \
  --chromium /path/to/pinned/chrome-headless-shell \
  --build-dir /path/to/verified-first-build \
  --opencode /path/to/pinned/opencode-2.3.2
```

The owner liveness and worker exec bridge are synthetic; the private native
process, PAKE, TLS and browser cookies are real. No real vault, enrollment,
workspace owner attach, Studio relay, Podman, VM or production listener is
used. This does not enable named mode, change the existing guest link, or
establish live owner-relay, security-review or publication acceptance.

### Authenticated named Studio browser composition

`tests/smoke_named_studio_https_browser.py` uses the real reserved TLS
dispatcher and an unmodified, pinned OpenCode 2.4.1 in a private loopback-only
network namespace. A synthetic owner and already-authenticated app grant
isolate the post-login path. Chromium navigates to the pinned first-party
document, fetches the real recent native session catalog, clicks a session,
and displays its real native text projection. It also checks that the
named page has no native iframe or script-readable cookie and keeps an
empty composer disabled. The fixture replaces only native exec and owner
provenance at the boundary; no provider/native agent runs. This smoke does
not verify PAKE login, browser Send, live Podman, or launcher deployment.

#### Opt-in named Studio composer Send

The same smoke's `--prove-send` phase separately exercises one trusted
Chromium editor input and Send click through the actual reserved workspace
TLS dispatcher. The default invocation remains provider-free and read-only.
With the same disposable nonroot loopback-only namespace and already
available Playwright interpreter described above, run:

```sh
unshare --user --map-root-user --net sh -c '
  ip link set lo up || exit
  fixture_home=$1; shift
  exec unshare --user --map-user=1000 --map-group=1000 \
    env -i HOME="$fixture_home" PATH=/usr/bin:/bin PYTHONPATH=src "$@"
' sh /path/to/empty-private-test-home /path/to/offline-playwright-python \
  -B tests/smoke_named_studio_https_browser.py \
  --chromium /path/to/pinned/chrome-headless-shell \
  --opencode /path/to/pinned/opencode-2.4.1 --prove-send
```

Only the fixture starts a fixed HTTP provider on `127.0.0.1`. Fresh native
configuration permits only that provider, denies tool use, and imports
synthetic history. The provider accepts one bounded request for the fixed
model and exact selected fixture's two user messages, and returns only a
canned text stream. Native 2.4.1 can advertise bounded tool definitions
even with the no-tools settings; the fixture issues no tool call and
requires a tool-free native transcript.

The proof observes the exact production `resume:true` POST bytes without
rewriting them, and checks both the browser queue result and the exact
native-acknowledged message ID. Native export verifies the persisted
user/assistant text and an unchanged neighboring session. After checked
site/relay drain and graceful native shutdown, it reopens the same private
fixture store with provider use disabled, verifies the retained turn
again, and requires another clean native exit. The provider is reaped
and the temporary browser and native roots are removed. No real provider
credential, history, account, or configuration is used.

Chromium, HTTPS dispatch, native queue admission, native execution and
export are real. Owner provenance, the post-auth grant, PAKE backend and
outer Podman exec are fixtures; the wire observer delegates to the original
Studio Send and socket write. This is one scoped synthetic Send proof, not
PAKE, live Podman, launcher, arbitrary-model, or replay acceptance.

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
- **Exact named native wire:** `tests/smoke_web_named_native.py` passed with
  unmodified Linux x64 OpenCode 2.3.2 (`e6543f6e5c39c40971ba38afd1819ac852c29ac735b5a89065d3d148d47f27ad`)
  and 2.4.1 (`ec93043f18f8a6b4d8e71ec9260e292ba6f91b66b142782135d40f0b58b0c152`),
  both SHA-256. It ran the named readiness worker, the held Unix control lease
  handshake and the providerless pairing worker against fresh private
  services. It verified the empty chunked pairing redirect, scoped cookie,
  one-use redemption, readiness/pairing refusals for a declared version
  mismatch, and relay projection for `/api/info` and idle session metadata.
  The container facts and exec boundary were synthetic, and the relay
  fixture connected directly to its disposable local native service. This
  is native wire evidence, not Podman, trusted-browser, or full Studio
  acceptance. The Studio transcript adapter remains independently pinned
  to 2.4.1.
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
