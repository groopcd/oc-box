# Named Studio browser controller (not served)

`web_named_studio_client.mjs` adapts the existing first-party Studio shell to
the fixed named workspace selection, transcript, and Send exchanges. It adds
no route, authentication, browser grant, or native-frame behavior. The module
is explicitly packaged alongside the shell, transcript and composer assets;
inclusion does not load it.

## Handoff for a future named caller

1. In the authenticated first-party document, load the reviewed Studio assets
   and initialize the existing shell, as described in the
   [shell composition contract](web-studio-shell-composition.md). Pass the
   server-injected exact `{page, generation: 0, sessionID: null}` to
   `createNamedStudioController(root, bootstrap)`. Do not construct it from
   routes, catalog rows, events, or storage. The module requires a top-level
   secure `https://workspace.ocbox-app.test:<reserved-port>` document with no
   controlling service worker and attaches only once per document. Invalid or
   missing bootstrap, shell, or styles leave the fallback unchanged.
2. The trusted shell must call `choose(event, exactSessionID)` or `home(event)`
   synchronously from its own explicit click action. Bind the ID in trusted
   shell code, not in event details or an attribute read by this module.
   Keyboard activation of an actual button can produce the same trusted click.
   The event gates a user gesture, not authorization. The controller does not
   subscribe to application events to start requests.
3. Both actions clear prior local state and abort the previous exchanges
   before submitting a fresh sequence. `true` means an exact, current server
   selection reply was accepted, not that a transcript is visible. Call
   `await client.present()` after a successful choose when the existing shell
   has independently verified the same native selection. A native route
   readiness notification alone is not a choose. Home never reads a transcript.
   Normal fallback navigation remains the shell caller's responsibility.
4. On observed native route/document, owner, or admission invalidation call
   `client.invalidate()` synchronously. Do not retain the old binding during
   an away/back transition even to the same ID; only an explicitly associated
   native readiness transition for the same pending choose can continue it.
   The controller also invalidates on visibility loss, pagehide and observed
   parent history/hash changes. These signals can only revoke local state;
   they never POST Home, choose again, or re-present a view. Use `dispose()`
   to remove its listeners when tearing down. Cancellation is not recall of
   an already delivered request. A future host composition must still enforce
   its own admission, deadlines, sequence ordering and revocation.

## Fixed wire behavior

Selection is `POST /oc-box/studio/selection` with exact
`{page, sequence, sessionID}` (`null` for Home). Client sequence and returned
positive server generation are distinct. A reply is accepted only for the
latest action, the exact page and intended session, with a generation higher
than any previously acknowledged and no higher than its action sequence.

The scoped `GET /oc-box/studio/conversation?session=<exact-ID>` and
`POST /oc-box/studio/send` carry only the dedicated page/generation assertions
`X-OC-Box-Studio-Page` and `X-OC-Box-Studio-Generation`, in addition to normal
browser headers. Send posts exactly `{sessionID, text}` and is private to the
real composer's trusted action path. `writable` enables that action, not
native queue permission. Fetch uses same-origin mode and credentials, no
referrer or cache reuse, and disallows redirects. No token is read from the
cookie jar or sent to the native frame. Drafts are not stored by the controller.

Check the exact response URL, media type, bounded content length and body,
strict UTF-8 and the server's compact JSON profile before using a reply. The
compact round trip rejects duplicate fields. The transcript view independently
validates the display projection. Each exchange has a seven-second local
bound. Only the exact `403/refused`, `200/queued` and `503/unconfirmed` status
and JSON pairs are meaningful for Send. After dispatch, a malformed, missing,
expired or stale reply is **unconfirmed**, never permission to retry. An
acknowledgement confirms queueing only. No automatic replay, alternate
transport or fallback Send is implemented.

## Offline browser scope

```sh
PYTHONPATH=src uv run --offline --no-project --with playwright python -B \
  tests/smoke_web_named_studio_client_browser.py --chromium /path/to/chromium
```

Chrome Headless Shell 159.0.8154.6 verified the real first-party modules with
intercepted synthetic requests and replies in a disposable browser home. It
checked trusted activation and replay refusal, request mode/options and
observable headers, strict Send classifications, aborts, an expired reply,
and native-frame draft preservation. Interception does not expose final
Fetch Metadata wire headers. No real TLS listener, app grant, native
admission, provider, or guest compatibility was exercised.
