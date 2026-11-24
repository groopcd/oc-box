# Studio legacy guest admission (unserved)

`web_studio_guest.GuestAdmission` is an in-memory admission registry for a
future first-party Studio adapter. It does not open a socket, acquire an
owner lease, serve a route, or modify the native one-use link. The existing
HTTP loopback gateway, native app and named HTTPS sign-in are unchanged.
There is no guest Studio transcript, selection, or Send dispatch here.

## Bind the original gateway or do not activate

The trusted adapter creates one `GuestBinding` after reserving the **original**
legacy gateway's IPv4 loopback listener and independently verifying the owner
lease it already holds. Use the exact actual `http://127.0.0.1:<port>` origin
from the reservation, with an explicit canonical decimal port (1–65535).
Missing/default shorthand, aliases, another port, HTTPS substitution and
proxy-rewritten origins are not reconciled. A browser that omits an explicitly
bound default port in its Origin cannot confirm. The binding holds the
verified workspace ID, immutable container ID and owner generation, plus its
own random gateway generation. It is an opaque server-side identity; equal
looking fields or a reused port do not recreate it. One binding can create
only one registry, even after closure. It is not a `WebBinding`.

Pin two synchronous trusted callables at setup. Neither is supplied by the
browser, and both must honor the absolute monotonic deadline and registered
use cancellation:

- `observe_gateway(binding, *, use, deadline)` must recheck the *same*
  reserved listener, gateway instance and verified owner lease, including its
  immutable container, generation, live native service and lifecycle fences.
  Return the original `binding` **object** or `None`. Never rediscover,
  reconnect to, or substitute a new owner or listener. A missing, different,
  or uncertain observation fences the registry permanently.
- `verify_native(binding, *, use, headers, authority, deadline)` must ask the
  unmodified native server behind that exact original gateway to authenticate
  the **browser's native Cookie**, on the registered native transport. The
  supplied bounded `headers` is a bytes block compatible with
  `web_sessions._authenticated(child, headers, authority)`: a request line
  `GET /api/info HTTP/1.1`, followed by the exact `host`, `origin`, and `cookie`
  fields, without a terminating blank line. `authority` is the exact gateway
  `127.0.0.1:<port>`. The native auth exchange sends that fixed GET with
  `Connection: close` and requires a positively authenticated native response
  on the pinned transport. For the actual supported native build, establish
  that this endpoint refuses missing and invalid cookies; a 200 from a
  public endpoint is not proof. The adapter must positively retire the exact
  native verifier worker before returning the original `binding` object.
  Denial or uncertainty returns `None`; a boolean (including `True`), cookie
  spelling, browser claim, bootstrap token or previous auth check is not
  evidence. Pass no owner Basic auth, named cookie, vault credential,
  discovery or retry; do not retain, log, reflect, or serialize raw native
  headers, cookies, or response bytes.

The current legacy attach reply, a same-user PID/socket, and the optional TUI
report generation do **not** prove the immutable owner run. Likewise,
`Gateway.url` alone does not pin the original listener. Today's
`web_sessions._authenticated` accepts both Cookie and Authorization; it
doesn't establish the owner or enforce a passed cancellation/deadline and
confirmed worker retirement. The legacy `Gateway` itself does not provide a
positive drain of all accepted browser workers. These are still-unmet
integration prerequisites. Do not call the old helper or trust a legacy
`AttachedControl` as a substitute for them, invent a named owner/grant, or
route to a standalone backend on failure.

## One-use confirmation and page admission

1. Keep the existing native pairing flow. From the original gateway's
   first-party confirmation document, `offer(binding)` creates a short-lived,
   one-use challenge, not authentication. Deliver it only in no-store
   first-party document state. After an explicit user confirmation, the
   document makes a same-origin `POST` to the **reserved, currently unserved**
   `/auth/oc-box-studio-guest/confirm`; use `mode: "same-origin"` and
   `credentials: "same-origin"`, with exact `Content-Type: text/plain` and the
   challenge as the complete body. No auto-confirm, URL token, CORS or
   wildcard origin. Fetch Metadata is not evidence of a human gesture or
   protection against a compromised script on this same origin.
2. The ingress must classify only that exact target on the original
   reservation and fully frame one bounded request before `confirm`.
   Preserve header multiplicity until validation; don't merge duplicates,
   rewrite Host/Origin, accept pipelining, or proxy through another origin.
   The gate requires exact Host and Origin, same-origin Fetch Metadata
   with an empty destination, exact length, and one nonempty Cookie field.
   It rejects alternate framing, proxy/auth headers and ambiguous or
   oversized input. A well-framed attempt with a live request budget
   consumes its challenge before native work, even if denied or limited.
   Invalid origins cannot burn it. Deny a claimed confirmation route
   uniformly; do not fall through to native, named or host routes.
3. Before `confirm` or `start`, bind a nonblocking, thread-safe `cancel` to
   that *exact* browser exchange and any native worker it will use. It must
   be safe during startup and after retirement and must not capture request
   credentials. `confirm` returns `None`
   for an unregistered refusal, or `(use, bootstrap_or_none)` for a
   registered exchange. Only after owner checks around positive native
   authentication does the gate mint a distinct app/page pair. Deliver
   only `bootstrap.app` and `.page` in protected no-store first-party
   state. `bootstrap.grant` is a server-side exact object, not a wire value.
   Recheck `current(use)` during bounded delivery. If delivery fails, retire
   the grant with `revoke(binding, bootstrap.grant)`. A registered refusal
   is not authority for any additional native work.
4. Every later first-party Studio exchange must independently enforce its
   exact method, route and same-origin policy, then supply both bearers to
   `start(binding, app=..., page=..., cancel=..., deadline=...)`. Keep the
   pair in the confirming document's memory, out of URLs, cookies, shared
   web storage, native iframe and logs. A new document must confirm anew.
   Only a returned `(use, grant)` with the exact live grant permits work;
   `(use, None)` is an already registered refusal. Use `current(use)` before
   and during native startup and downstream delivery. It is not an atomic
   guarantee around an external write. A subsequent selection adapter must
   separately verify exact session access and page selection, without
   inferring authority from a native focus report or a request ID.

## Bounds and lease teardown

- Defaults are 32 pending challenges, 64 live app/page pairs, 32 registered
  exchanges, and 12 confirmation attempts per rolling minute. Configured
  caps cannot exceed 128, 256, 128 and 128 respectively. Challenges and
  grants have absolute monotonic lifetimes (30 seconds and 15 minutes by
  default, maxima 60 seconds and one hour), with no refresh on use. A use
  has at most ten seconds for request work, further limited by the caller
  deadline and the applicable challenge or grant expiry. No capacity
  eviction or token replay/fallback restores authority. Pending cancellation
  delivery also reduces available work capacity. The registry indexes only
  bearer digests, not native credentials.
- Call `expire()` from the original lifecycle timer, including during idle;
  observe owner loss independently of traffic and close the registry on
  loss, shutdown, or uncertain owner/lifecycle state. `revoke` removes and
  cancels only the exact guest grant, not the native sign-in. The registry
  cannot detect native logout unaided. Before activation, the adapter must
  independently enforce native authentication continuity on its protected
  routes or wire a verified revocation fence; if it cannot identify the
  affected grant, fence the guest registry rather than carry it over.
- For **every** registered `use`, including refusal before delivery, call
  `finish(use, confirmed=True)` only after actual downstream and native
  retirement (including startup cancellation). Mere cancellation, a timeout,
  local EOF without required worker evidence, and a cleaned-up host-side
  process without verified native cleanup are not proof. Report uncertainty
  with `confirmed=False`; that fences the registry and leaves drain unconfirmed.
  Do not finish while callbacks or its exchange can still start or do work.
- Fence ingress on shutdown, call `close()` to cancel outstanding registered
  uses, and require `wait_drained(...) is True` **and** independent positive
  retirement of the original gateway listener and all its workers before
  releasing the original owner lease. The module cannot release the lease.
  An unconfirmed drain or a raised cancellation callback is not success.
  Revocation and cancellation cannot recall bytes already delivered.

Guest grants expose no host management, provider credentials, terminal,
Podman, backups, logs, named app, or other workspace authority. The offline
tests use synthetic callbacks and identities; they establish neither native
authentication behavior nor gateway, browser, lease, or host acceptance.
