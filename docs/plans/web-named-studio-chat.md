# Named Studio chat dispatcher (unserved)

`web_named_studio_chat` composes the existing pinned OpenCode **2.4.1**
[conversation reader](web-studio-read-route.md) and
[explicit text Send adapter](web-studio-send.md) with the actual named app and
its [page selection registry](web-studio-selection.md). No HTTP parser,
listener, route table, bootstrap, UI, launcher or site lifecycle is activated
or changed. There is no guest, 2.3.2, or native-proxy fallback.

## Exact browser envelopes

Only a framed HTTP/1.1 request from the reserved workspace HTTPS worker is
eligible. `Host` must be the exact workspace authority; the actual app
`__Host-ocbox-app` cookie must resolve to a current `BrowserSession` from that
app's `BrowserAdmission`. Both routes require:

- `Sec-Fetch-Site: same-origin`, `Sec-Fetch-Mode: cors` or `same-origin`,
  and `Sec-Fetch-Dest: empty`. No navigation, iframe, cross-site, or
  same-site sibling-origin requests.
- `X-OC-Box-Studio-Page`: the exact 43-character opaque page nonce.
  `X-OC-Box-Studio-Generation`: the exact current server generation, in
  canonical positive ASCII decimal, at most `2^53 - 1`. These only assert a
  match to a live, independently approved page view. They are not grants.

| Request | Exact requirements |
| --- | --- |
| `GET /oc-box/studio/conversation?session=<exact-ID>` | Empty body; no content type; `Content-Length` absent or `0`. `Origin`, if sent, must match the workspace origin. An ordinary same-origin fetch that omits it still needs all three Fetch Metadata fields, the live app grant, and the exact page view. |
| `POST /oc-box/studio/send` | Workspace `Origin` is mandatory. Exact `Content-Type: application/json` and canonical matching `Content-Length`. Strict UTF-8 JSON with exactly `{"sessionID":"<exact-ID>","text":"..."}` and no duplicate fields. The body is at most 8 KiB; the existing Send text rules allow at most 4 KiB UTF-8. |

Targets are matched literally before any path/query decoding. The existing
named header validation rejects duplicates, forwarded/authorization headers,
and mismatched `Referer`. Transfer/encoding/upgrade fields and idempotency
headers are not accepted. No browser cookie, header, native option or route is
forwarded. A future first-party UI must submit the POST only for an explicit
Send action; these HTTP checks cannot attest a physical gesture.

## Trusted TLS-worker composition

Construct `NamedStudioChat(services._owner, selections)` only after the actual
named app is composed. It borrows that binding's existing `VerifiedOwnerLease`
(including its original attach control socket) and app admission. The registry
must have been constructed for the exact `lease.identity` object and that same
admission. Do not copy/recreate the owner, lease, admission, or page view.

The existing workspace TLS worker later supplies a bounded parsed `NamedRequest`,
`actual_tls=True` from its verified TLS context, its absolute monotonic
`deadline`, and these trusted callbacks:

- `check()` rechecks the actual worker/site fence. The dispatcher additionally
  checks the same named owner/account and original page view; a newer selection
  or a new sign-in is never adopted during a request.
- `write(reply, deadline, *, check)` emits a single host-owned `ChatReply`.
  Call the supplied check before **each bounded write**. Frame only its fixed
  status, headers and body; the separate Studio limit is `MAX_RESPONSE_BYTES`
  (512 KiB), not the metadata writer's 8 KiB. No native headers or cookies,
  response substitution, deferred delivery, or reuse of the peer.
- `interrupt()` nonblockingly wakes only that exact downstream request on
  grant cancellation; do not close, unwrap or transfer the TLS socket.
  `retire()` nonblockingly returns `True` only after no downstream work remains
  for this borrowed exchange. The site still owns the TLS socket and closes
  it in its normal worker exit path. Retirement is not site drain.

The managed read adapter owns its grant-use registration. Send installs
cancellation on the same admission before it executes, and keeps the use
through native cleanup, checked delivery and downstream retirement. If
cleanup cannot be confirmed, the affected use stays charged and the admission
is fenced; the outer lifecycle must honor the failed drain and retain its
original owner/reservation. No socket or owner is closed by the dispatcher.
`respond` is synchronous and returns no second reply. A fixed
`NamedRequestError` means **close only**, even if writing may already have
started. Do not retry, fall back to another handler or emit a second error.

## Send result is per request

The only Send replies, and only while the original request is still live with
confirmed native transport cleanup, are:

| HTTP / exact JSON | Meaning |
| --- | --- |
| `403 {"status":"refused"}` | This operation did not attempt the native POST. |
| `200 {"status":"queued"}` | This operation verified the native queue acknowledgment. It does not confirm execution, a reply, or completion. |
| `503 {"status":"unconfirmed"}` | This operation attempted the native write but could not verify admission. Input may already be queued or executing. |

If the exact grant, owner, selection, deadline, cleanup or response delivery
is lost, the browser may get no usable reply. A missing, truncated or
unrecognized Send response is not proof of refusal; do not retry
automatically. Inspect the exact conversation in native OpenCode first. This
dispatcher does not establish a launch-wide Send acknowledgement policy,
HTTP replay protection, idempotency or exactly-once behavior. No release or
public activation follows from the synthetic tests.
