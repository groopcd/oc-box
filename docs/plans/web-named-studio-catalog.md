# Named Studio recent-session catalog (unserved)

`web_named_studio_catalog` accepts only `GET /oc-box/studio/catalog` with an
empty body, no query (not even `?`), and the actual named workspace app grant.
It is a **recent list of up to 16 sessions** in native descending list order,
not a complete workspace scan, last-activity ranking, native tab list, or
report of TUI membership or focus. No gateway, TLS route table, UI, launcher,
or product defaults are changed by this source. A future UI should label the
window “Recent sessions (up to 16)” and distinguish “more or unknown”.

## Exact scope and projection

- The original `NamedOwnerBinding` supplies the exact `VerifiedOwnerLease`,
  bound `WebBinding` and app `BrowserAdmission`. Only stable native **2.4.1**
  is accepted. There is no guest or other-version fallback.
- Read `/api/info` and `/api/location` on that private owner, with the exact
  named app's existing server-side native cookie. Do not copy any browser
  native cookie or headers upstream. Check the named native identity and the
  exact registered *internal* directory, project ID, root, canonical
  directory and expected subpath. Read exactly one
  `/api/session?directory=<quoted-internal-directory>&order=desc&limit=16`
  page, then recheck the same native identity/location. No caller path,
  caller cursor, cursor follow-up, arbitrary route, child lookup or
  per-session GET is allowed.
- Check the exact project, location and subpath of every row. Reject
  duplicate IDs, malformed types, inconsistent scope and unexpected
  pagination envelopes. No partial result on a refusal.
- Return only `{"sessions":[{"id":"...","title":"...","archived":false,"child":false}],"hasEarlier":false}`.
  `archived` and `child` are booleans derived from native archive time and
  parent ID; neither underlying value is returned. Titles are literal text
  (never HTML), clipped to 160 UTF-8 bytes with control/format normalization.
  Known bound credentials are not allowed in displayed fields; this is not
  general redaction of user-authored titles. No native path fields, project
  metadata, cursors, headers, credentials, or transcript bodies are exposed.
- `hasEarlier: true` means **more or unknown**: the page was full or native
  offered a continuation. In 2.4.1 even a short final nonempty page can have
  a continuation cursor. False means this bounded response offered no such
  signal, not independently verified inventory completeness.

A row says only that the native list associated it with this directory during
the bounded read. It does not authorize a click, prove historical provenance,
or imply that Studio can read or Send to that session. Use the independent
exact-session [selection verification](web-studio-selection.md) on any click,
including the moved/reverted/forked-session eligibility checks. The list and
the before/after checks are not a native transaction.

## Trusted workspace TLS-worker handoff

Construct `NamedStudioCatalog(services._owner)` only after the actual prepared
named app has been composed. The future caller must admit the exact target in
the workspace-only TLS transport and hand its already-verified worker socket,
parsed `NamedRequest`, absolute monotonic deadline and live site `check()`
to `respond(stream, request, deadline, check=check)`. The outer TLS worker
continues to own the socket and close it after the one exchange.

The handler requires exact `Host`, the live `__Host-ocbox-app` grant, and
same-origin Fetch Metadata (`site=same-origin`, `mode=same-origin` or `cors`,
`dest=empty`). `Origin` may be absent on same-origin GET; if present it must
match exactly. Framing, duplicates, forwarded headers, `Referer`, upgrades
and encodings use the existing named fences. No page selection is adopted
or changed by a catalog request.

The handler starts no service or new worker implementation: only the
existing providerless managed relay on the leased immutable container and
private endpoint. It registers actual browser use before connector startup,
wakes the borrowed browser/relay sockets on cancellation, requires strict
reply framing/EOF and exact relay reap before writing, and rechecks the
grant, owner and site through the checked named JSON write. The existing
per-reply native cap is 32 KiB and the browser JSON fits the 8 KiB named
response limit. There is no refresh, retry, or automatic fallback. Unknown
cleanup keeps the use charged and fences app admission; it is not confirmed
drain or permission to release the lease. A refusal is close-only; never
send a second response if writing may already have started.

## Verification

```sh
PYTHONPATH=src python3 -B -m unittest discover -s tests -p test_web_named_studio_catalog.py -v
python3 -B tests/smoke_web_named_studio_catalog_v2.py --opencode /path/to/opencode-2.4.1
```

The tests use synthetic browser grants and private socket fixtures. The pinned
native smoke uses a fresh isolated catalog including a same-project sibling
directory, replaces only the Podman exec boundary for pairing and relay, and
requires exact managed relay exit. It establishes no browser, live Podman,
provider or deployment acceptance.
