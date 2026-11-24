# Studio conversation route backend (unserved)

`web_studio_read.respond` handles one already authenticated first-party
`GET /oc-box/studio/conversation?session=<exact-ID>` with an empty body.
No decoding, optional query fields, browser-provided cursor, native routes or
write operations are accepted. `ReadRequest` contains only the method, exact
target and body. A successful `ReadResponse` contains bounded UTF-8 JSON with
the [2.4.1 plain-text projection](web-studio-conversation.md) and fixed
first-party headers, including `no-store` and `nosniff`. A refusal raises a
fixed `ReadError`; it does not forward upstream status, headers, credentials
or diagnostics. If writing may have started, close rather than writing another
response. Render the projected text literally, not as HTML.

## Trusted composition seam

- **Named grant:** Only the reserved workspace HTTPS worker may compose this,
  after validating the exact request, same-origin/browser policy and a live
  app grant. Supply the actual app `BrowserAdmission`, `WebBinding` and looked-up
  `BrowserSession` from that admission (not the cookie string or native
  pairing data), together with the original `VerifiedOwnerLease`. The backend
  enters a use on that same registry. Its `ReadScope` is produced by trusted
  workspace/Studio state, not from request query fields or native focus
  guesses. `observe(deadline)` must recheck the same shell's approved exact
  session, browser identity and selection generation, including away/back
  changes; any site/authorization fence must be reflected in that observation.
  The backend separately rechecks the exact immutable owner identity and
  borrowed attach socket. Neither the scope nor a socket alone authenticates
  the caller. There is no named dispatch or selection-state wiring here.
- **Native one-use guest:** The present loopback gateway's native-cookie check
  is authentication, not an actual registered, cancellable use covering this
  exec and the browser exchange. It also does not supply the required trusted
  generation/selection observer. Do not synthesize a named HTTPS binding, grant
  or owner from a successful guest check, and do not call the standalone
  projection as a fallback. Guest composition needs its own real grant and
  owner/selection lifetime with equivalent cancellation and drain guarantees.

The trusted writer synchronously frames this single response and calls the
provided `check` before each bounded downstream write. It must not queue or
retain the result or reuse the request stream. A nonblocking `interrupt` wakes
that exact stream on revocation; `retire` must positively confirm downstream
retirement before releasing the grant use. The named metadata writer's
8 KiB response limit is not the Studio projection limit; a future Studio writer
must preserve its own bound without widening metadata routes.

The worker uses the pinned container ID and the existing providerless client
command. It waits for a private start byte after grant/owner/selection checks
and checks for cancellation between native GETs. In-flight native I/O retains
the existing hard four-second worker bound. Host delivery uses the earliest
of the request deadline, grant expiry and six seconds; short bounded cleanup
may outlast delivery. The host requires a managed terminal record, strict EOF
and a real zero exit of the exact exec. A startup ambiguity, lost terminal
record or host-only force kill is **not** confirmed drain. Unconfirmed worker
or downstream retirement fences the admission and leaves the use charged;
the outer lifecycle must honor that failure and not release the owner as if
drained. Cancellation cannot recall bytes already delivered.

The synthetic checks exercise the actual grant registry, managed subprocess
boundary and an isolated unmodified 2.4.1 native projection (locally replacing
only Podman exec). They do not establish guest/named browser integration,
deployment, interactive, or external-host Podman acceptance.
