# Browser workspace runtime rollout

The experimental `oc-box web --workspace-layout` path adds Studio, Dashboard,
Chat focus and a metadata-only browser manager. It retains one unmodified
OpenCode iframe and the existing localhost one-use pairing. Open TUI tabs and
project history are distinct; history is an explicit bounded read of the
registered directory in the leased private service. The browser has no host
registry, global catalog, host logs, storage operations or backup authority.
It does not start a manager terminal or duplicate Textual. See the
[CLI reference](../cli.md#web--browser-interface) and [shell contract](../web-shell.md).

The guest-only release does not expose host certificate preparation or
diagnostic commands. Their prototypes and tests remain in the source tree
for future development. They do not enable a TLS listener, enroll accounts
or change native pairing. A successful local certificate check is not
evidence of trust in a browser profile.

## Named sign-in is not enabled

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
Cancellation stops this client's transport; it cannot undo a POST already
processed by the ingress. A successful response yields only a one-use
in-memory ticket carrier, not app admission.

The session interface is for a trusted, independently reviewed WASM binding
that prepares the password locally, verifies the server proof before
emitting KE3 and confirms cleanup. The adapter does not validate artifact
provenance or establish memory erasure. The Node fixture checks both origins
and the exact Python wire format with a synthetic session that checks its
arguments; it does not verify a password, make browser requests, or establish
browser trust. Run it from a source checkout with
`PYTHONPATH=src python3 -B -m unittest discover -s tests -p test_web_pake_client.py -v`.
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
