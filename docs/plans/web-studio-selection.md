# Studio page selection registry (unserved)

`web_studio_selection.SelectionRegistry` is in-memory state for one exact
named workspace owner and its actual `BrowserAdmission`. It makes no native
calls, starts no workers, and does not infer a selection from native
tabs, global focus, history, a URL, or a request ID. It stores only the live
page nonce, exact browser grant identity, expiry, order counters, and the
last approved exact session (or no selection). No drafts or transcripts enter
the registry.

## Named first-party composition

Use one registry for the app's original `VerifiedOwnerLease.identity`,
`BrowserAdmission`, and `WebBinding`. Supply a bounded `observe_owner(deadline)`
that rechecks the same verified owner/lease and returns the exact current
identity or `None`. Never look up or adopt a replacement owner. The reserved
workspace HTTPS worker still enforces the site's request, origin, authorization
and framing policy, obtains the actual `BrowserSession` from that admission,
and owns the real use registration, cancellation and drain.

1. **HTML/bootstrap.** After first-party authentication, call
   `bootstrap(browser, deadline=...)` for a new page document. Recheck that
   exact view during delivery with `observe(view, deadline)`. Use only
   `view.public_state()` for the no-store bootstrap payload:
   `{"page": "<nonce>", "generation": 0, "sessionID": null}`.
   Never serialize the view object, grant or `ReadScope`. Keep the page nonce
   in this document's private first-party state, not in URLs, shared storage,
   logs or native iframe state. It is an opaque page locator, not a grant or
   a substitute for same-origin policy.
2. **Selection change.** A separate exact, same-origin first-party POST can
   pass a bounded envelope with only the page nonce, an integer change
   `sequence`, and the requested exact session ID (null means Home). The
   page starts its sequence at `1` and increments it for every explicit change.
   The handler registers the real browser use, then calls `change` with an
   independent `verify(owner, browser, session_id, deadline)`. That callback
   must verify this exact session is allowed for this browser in this
   workspace, through the same verified owner and registered cancellable
   request, and return the exact verified ID. A request value, same ID in a
   different project/directory, native focus report or cached client claim
   cannot supply that proof. Positive proof obtained asynchronously commits
   only if this exact page/change/grant/owner is still current. Recheck the
   returned view during delivery and use only `view.public_state()`.
3. **Conversation GET.** Authenticate first, then use `snapshot(browser, nonce,
   session_id=..., generation=..., deadline=...)` to match browser assertions to
   the approved view. A future adapter can carry the nonce and the canonical
   decimal server generation in single bounded first-party headers
   (`X-OC-Box-Studio-Page`, `X-OC-Box-Studio-Generation`), separate from the
   existing exact `?session=<ID>` read target. With the original owner lease
   and same admission, pass `view.scope` to [the read backend](web-studio-read-route.md)
   and bind its `observe` callback to **that view**, not to a lookup of the
   latest selection. For example, wrap `registry.observe(view, deadline)` in
   the same request/site fence before and after the check. The read backend
   registers its own actual use and keeps it through worker and delivery
   cleanup. A snapshot is not admission to do I/O on its own and grants no
   Send or other native authority.

The client change sequence orders intents; the server generation counts
accepted transitions. An intent with a higher sequence invalidates the old
view **before** verification, even if the ID is the same. A refused verifier
leaves no selected view and consumes that sequence. Gaps are allowed; equal
or older sequences refuse. An old generation, late response, another page
with the same ID/generation, or away/back to the original ID cannot restore
an old view. Future clients must ignore out-of-order replies, hide old
transcripts while changing selection, and report Home/away explicitly. This
does not infer unreported browser navigation or recall already delivered text.

## Bounds and teardown

- The page cap defaults to 128 and cannot exceed 1,024. Full capacity refuses
  issuance without displacing live pages. An absolute page TTL defaults to 30
  minutes, is at most one hour, and cannot outlast its grant. Reads and changes
  do not renew it.
- `retire` permanently removes only the exact authenticated page. `expire`
  is called by the existing owner timer to sweep expired or revoked pages;
  operations also refuse them without a timer. Retiring does not revoke the
  grant. The registry retains one bounded, strictly increasing issuance
  ordinal so retired nonces cannot be reused, even if entropy repeats; no
  replay tombstone eviction or nonce recycling occurs. Sequence or ordinal
  exhaustion refuses, without wraparound.
- Close the registry on owner/site teardown, independently of authenticating
  a browser request; owner loss also fences it permanently on observation.
  Never replace or reset it under the same live owner/app lifetime. Normal
  admission revocation still cancels registered uses; the registry also
  checks the exact live grant on issuance, selection commit and every
  observation. Independent access-policy changes must revoke the affected
  grant, retire the affected page, or fail the request/site guard; the
  registry does not discover external policy changes. The outer lifecycle
  must still confirm drain before releasing the owner. Registry closure
  is not transport cleanup.

## Separate guest prerequisite

Do not pass the guest loopback's native cookie or invent a named HTTPS
`WebBinding`. A future one-use guest composition needs a first-party guest
grant issued only after verified native pairing and same-origin confirmation
on the original loopback gateway. Its in-memory admission must bind the exact
gateway/listener lifetime and origin, verified workspace and immutable
owner generation, and a distinct guest browser-grant object. Each HTML,
selection and read exchange must authenticate that same grant and register
its exact downstream and native worker for cancellation and confirmed drain
on logout, expiry, gateway close or owner loss. Only an adapter providing
those object-identity, selection-verifier and lifecycle checks could back a
guest page registry. The current native-cookie check and gateway alone do not
provide them. No guest or named routes are enabled by this source.
