# Browser workspace runtime rollout

The experimental `oc-box web --workspace-layout` path adds Studio, Dashboard,
Chat focus and a metadata-only browser manager. It retains one unmodified
OpenCode iframe and the existing localhost one-use pairing. Open TUI tabs and
project history are distinct; history is an explicit bounded read of the
registered directory in the leased private service. The browser has no host
registry, global catalog, host logs, storage operations or backup authority.
It does not start a manager terminal or duplicate Textual. See the
[CLI reference](../cli.md#web--browser-interface) and [shell contract](../web-shell.md).

`oc-box web tls` supplies host-only certificate preparation and validation.
`oc-box doctor --feature web-tls-local` checks optional localhost tooling.
These do not enable a TLS listener, enroll accounts or change native pairing.
A successful local certificate check is not evidence of trust in a browser
profile. The default command and Textual remain unchanged.

## Named sign-in is not enabled

The source contains internal encrypted account records, bounded PAKE
orchestration and expiring browser admission with stream cancellation hooks.
It does not route a browser through these components. No password backend or
browser WASM is shipped. A candidate `opaque-ke` prerelease demonstrated
separate offline Chromium WASM and native interop with synthetic inputs;
that did not exercise TLS, live enrollment, cookies, origin validation or
workspace admission. See [web sign-in](../mockups/web-sign-in.md) for the
credential protection and deployment requirements.

The next decision is which **separate trusted sign-in origin** the operator can
configure. It cannot be the same origin that runs workspace-controlled code.
Changing ports alone does not separate cookies. For a network deployment, a
trusted account hostname plus a verified HTTPS workspace hostname is the
preferred design. Start with one loopback-only named relay per verified owner
and refuse concurrent guest access to that same owner unless independent
admission is proven. Guest mode remains the default elsewhere.

Before turning on experimental named sign-in, independently review the exact
PAKE build and applicable distribution terms, choose the certificate/hostname
and trust model, then test host-authorized enrollment, single-use browser-bound
handoff, native cookie security, client-scoped revocation and active stream
cleanup on the chosen browser and Podman target. No HTTP or plaintext-password
fallback. Only after the loopback gate passes should a separate explicit LAN
bind be considered. Host-wide manager features require their own authorization
design, not merely a stronger workspace login.
