# Internal staged named enrollment

This is the unserved host coordinator contract. It does not expose a route,
install an artifact, select an account, or grant access. Store publication
goes through the separate foreground host commit primitive.

The host chooses an exact `RegistrationReview` from the account store,
independently authorizes its account and workspace scope, and reserves a
live local `BrowserBootstrap.claim_enrollment(...)`. A reserved enrollment
cookie cannot initiate ordinary sign-in or app confirmation. Construct
`EnrollmentApproval(review=..., origins=..., claim=...,
registration_worker=..., proof_profile=...)` with a dedicated, sealed and
digest-checked `RegistrationWorker` (OCOR) and `LocalPakeProfile` (OCOP).
Successful construction transfers worker ownership. Never share them with
a sign-in owner or between approvals. The bootstrap claim and OCOP profile
must both match the two configured origins, including ports. The host
owns any admission cap across multiple approvals.

Each stage takes `account_origin` and the original **server-verified account
bootstrap nonce**. Do not select either from JSON; the route owner still
checks the actual TLS/SNI, exact Host and Origin, the account cookie, and
bounded canonical framing before dispatch. The browser does not choose the
review, account identity or scope.

| Stage | Additional inputs (bytes after transport decoding) | Success value |
|---|---|---|
| `start` | Blinded registration `request` (32) | `RegistrationResponse(challenge, response)`; response is 64 bytes |
| `start_proof` | Same `challenge`, `registration` (192), fresh `ke1` (96) | `EnrollmentProofChallenge(ke2, context)`; KE2 is 320 bytes, context is 32 bytes |
| `finish_proof` | Same `challenge`, `ke3` (64) | `True`, not a grant or handoff |

`start_proof` validates the canonical upload through OCOR before opening the
OCOP attempt. OCOP uses only that record and the original reviewed setup,
with the exact local credential, client and verifier identifiers. The
browser supplies no password or identifier override in these rounds. The
public proof context is SHA-256 over the following length-prefixed ASCII parts
(unsigned 16-bit big-endian byte lengths), in order:

1. `oc-box.named.local/OPAQUE-25519-SHA512-v1`
2. `enrollment-proof`
3. The exact account HTTPS origin, one literal LF, and the canonical
   unpadded 32-byte-base64url challenge **text** from `start`

This context is separate from login. It contains no cookie, private workspace
ID, setup, or account store data. The browser adapter must independently
derive the same context and verify the server's OPAQUE proof before emitting
KE3. A different origin, browser nonce, or challenge cannot consume the live
round. The rightful browser gets one attempt; a malformed frame or worker
refusal does not allow retry. The approval TTL and live bootstrap remain
authoritative; the pinned workers can impose shorter I/O and proof deadlines.

## Unserved HTTPS dispatch contract

`web_enrollment_ingress.EnrollmentIngress(origins=..., approval=...)` binds
one unused approval once, before its first round, and is not connected to a
listener. A direct worker round and an ingress cannot share an approval, and
neither an active proof nor a completed direct fixture can be rebound. The
future caller selects the dedicated account TLS connection (not request-role input),
checks exact SNI and loopback binding, and passes the raw literal target,
entire raw header list with duplicates, and original bounded request body.
`actual_tls=True` must mean that this connection completed the real account
TLS handshake; never derive it from request headers or an upstream proxy.

The ingress independently requires exact account `Host` and `Origin`,
and `Sec-Fetch-Site: same-origin`, `Sec-Fetch-Mode: same-origin`,
`Sec-Fetch-Dest: empty`. It uses the original `__Host-ocbox-account` cookie
directly against the verified enrollment claim, **not** the sign-in
app-nonce translation. It accepts `application/json`, exact decimal
`Content-Length`, and compact ASCII JSON in the following key order. All
binary fields use canonical padded standard base64; `challenge` is the
original 43-character base64url text. A rejected header or body never
calls a worker.

| Literal POST target | JSON keys in order | Exact request bytes | Success JSON |
|---|---|---:|---|
| `/enroll/start` | `request` | 58 | `challenge`, `response` |
| `/enroll/proof/start` | `challenge`, `registration`, `ke1` | 470 | `ke2`, `context` |
| `/enroll/proof/finish` | `challenge`, `ke3` | 156 | `{"verified":true}` |

Use the ingress ceiling (`MAX_BODY_BYTES`, 470) before reading a body; pass
one fully framed request and close it rather than attempting a second.
Invalid requests return fixed 403 JSON. Never fall through to ordinary
login. Success has fixed no-store headers and no token, cookie, redirect
or native access. Request headers, bodies, response protocol bytes and
worker details must not appear in logs.

### Trusted response writer and store handoff

Supply the real connection's absolute monotonic `deadline` and its trusted
`check()` (`None` or `True` on success; raise or return `False` on loss).
The coordinator checks the request around each worker call and again
before publication. OCOR receives the tighter request cutoff; OCOP keeps
its own shorter bounded I/O and independent proof-attempt lifetime across
connections. A synchronous worker call is not retroactively interruptible
by the request check. An observed loss prevents publication and fences
the approval.

An `EnrollmentReply` with status 200 is provisional, not delivery. On the final
request, pass the **original, already checked account TLS socket** as `stream`
to ingress. Successful KE3 and worker drain bind the original approval to that
exact reply and socket, before any writer is admitted. No late socket binding,
copy, replay, or second writer can acknowledge it. `reply.check_write()` only
checks liveness; it never acknowledges delivery.

`write_enrollment_reply` is the sole checked local write boundary for a future
enrollment-only TLS worker. It uses both the request and writer guards before,
during and after a complete bounded write, never extends the original cutoff,
and then acknowledges once against the original writer under the approval lock.
On failure, expiry or cancellation it cannot authorize the result. A dropped
pending reply can be aborted with `reply.abort_write()`; never retry the reply
or request. Cancellation racing an active writer fences publication and must
not be mistaken for confirmed writer drain. Acknowledgment cannot retract bytes
already sent or prove receipt by the browser. No normal named sign-in route calls
this writer.

Only the acknowledged delivery makes `take_for_host()` eligible. It returns
the original review, registration and `current()` guard once, while the
approval and claim are still current. None goes to HTTP. Cancellation after
acknowledgment via the approval itself revokes eligibility before handoff and
invalidates a previously taken result's `current()` guard. The consumed reply
cannot act as a second writer or abort a later phase. This is not a transaction
with future store publication.

Transport-free ingress and CDP fixtures may exercise the provisional JSON but
cannot later attach a socket or release a host result. Direct worker fixtures
use `take_for_fixture()` for round-trip bytes only, with no reviewed result or
CAS guard; neither synthetic path substitutes for checked HTTP delivery.

### Unserved foreground host commit

The host foreground can call
`web_enrollment_commit.commit_enrollment(approval, scope=held_scope)` once,
only after checked reply delivery. Pass the original approval, not a result,
reply, or callback that claims the browser succeeded. The required
`HeldEnrollmentScope(approval, review, current)` binds the exact **approval
and review objects**, not another browser proof, a review copy or a new
selection of the username, store, operation, or workspace IDs. An attempt on
the right approval before checked delivery retires it; a wrong approval or
review cannot redeem or cancel it.

The scope producer must independently verify every workspace in the review
and retain exclusion through the result (including failures). That requires
verified stopped/absent owners with restart excluded, or independently proved
exclusive scope with conflicting admission fenced and affected browser grants
revoked and drained. A disconnected socket, one stopped observation, ordinary
attach, or an account-generation check is not that proof. There is no
production scope producer, launcher integration, or HTTP binding for this
primitive. Its mandatory `scope.current()` is a bounded, read-only recheck of
an already held fence, not authority to unlock, stop, refresh credentials,
retry, or enroll through a callback.
Ordinary named sign-in teardown closes its bootstrap and invalidates claims;
the stopped-owner variant would need a separate enrollment-only bootstrap
lifetime rather than stopping the ordinary sign-in service after proof.

The host's main thread displays the exact store path, username, operation,
and full workspace scope on its controlling terminal. The operator must
submit `ENROLL` or `RESET`, then enter the **host web account store**
passphrase with reveal disabled. There is no programmatic secret input,
browser password, provider vault, dashboard private input, or stdin fallback.
Time spent in prompts does not extend claim or approval expiry. A denial,
lost fence, expired claim, stale store or unverified write spends the
attempt. Do not replay it or assume an unverified write did not happen.

The eligibility and scope guards are rechecked through the existing
`commit_registration` guard under its store lock. This does not replace the
locked comparison against the original ciphertext and review, nor make
approval cancellation a transaction with filesystem replacement. Do not
retire the approval before the guarded write; eligibility already required
the proof workers and writer to drain. The primitive retires the taken
approval at the end; it does not release the external fence or establish
native owner drain.

### Approval retirement

The owner calls `expire()` periodically and on bootstrap retirement. This
fences stale work and initiates bounded worker teardown. `close()` permanently
retires the approval and returns whether both workers and any in-flight call
have drained. Keep the outer teardown fence when it returns `False`.
`EnrollmentCleanupError` and `cleanup_failed` indicate uncertainty, and
the latter is sticky. A subsequent explicit `close()` may verify cleanup of
the **same** workers without reopening approval or replaying a round.

### Checkout-only browser enrollment binding

The existing pinned browser login artifact is unchanged. The separate
[enrollment research sources](../../tools/web-pake-research/README.md#optional-browser-enrollment-slot)
take `accountOrigin`, `workspaceOrigin`, `approvedUsername` and
`openAttempt` from trusted host-approved page configuration. Neither the
URL, the server's protocol replies nor the browser's enrollment input
chooses the account or an identifier. The source does not load an artifact
or provide a page, input form, or account chooser.

`createLocalEnrollmentClient(config).enroll({readPassword, signal})` accepts
a synchronous, one-shot local reader, not a password value or named form
field. The reader transfers an owned mutable view with 1–1024 UTF-8 bytes
to `openEnrollmentAttempt(verifiedModule)`. The enrollment slot keeps those
bytes in bounded WASM state while the original OCOR registration and a
fresh OCOP possession exchange run. It uses the same client/verifier IDs
for both operations. The transport derives those IDs and the exact context
independently, and sends only the canonical three JSON requests. It erases
accessible mutable buffers where possible and requires confirmed local
discard before sending KE3; a trap is not confirmed cleanup or guaranteed
erasure of all copies.

The result is only `true` for the fixed verified proof reply. Refusal,
timeout, page loss, cancellation or uncertain cleanup does not retry, call
ordinary sign-in, or receive a ticket. Aborting a browser fetch cannot
undo a POST already processed on the host. A trusted HTTP writer still
owns the guarded delivery boundary above; the browser never writes the
store. This research source and its local build do not authorize any asset
distribution, trusted UI or launcher activation, or a compiled PAKE pin.

## Host account snapshot for named-site preparation

Authenticate the existing exact account-store path once with
`web_accounts.load_snapshot(path, passphrase)`. Pass **that same snapshot's**
`store=snapshot.store` and `account_current=snapshot.current` to
`web_named_site_lifecycle.prepare_sites(...)`. Keep the snapshot open through
confirmed site drain; close it on startup refusal or after drain. A retained
callback returns `False` after `snapshot.close()`. This is a store-generation
check, not host approval, so independent owner and admission fences still
apply.

The callback retains only the original file/directory pins, the exact path,
metadata and ciphertext digest. It does not retain the passphrase or decrypted
setup/records, take the account-store flock, or do any key derivation. It
checks the exact no-follow path before and after bounded hashing of the pinned
ciphertext, never opens a replacement file for reading, and refuses closed,
missing, modified or substituted generations permanently. It cannot detect
every unobserved same-user race or offline rollback. A successful reviewed
enrollment or reset replaces the store and retires the old snapshot; loading
another generation is a new explicit authentication, not a fallback or live
refresh. Neither opening nor checking a snapshot writes account data.

The checkout-only `tools/web-pake-research/check-registration` fixture uses
the existing pinned OCOR/OCOP binaries through this unserved dispatcher.
It passes synthetic proof, altered KE3, wrong context or verifier, wrong
browser, replay, and bootstrap retirement tests without a listener, actual
TLS, or account store. Separate Python tests exercise the encrypted store's
stale-review refusal. `check_browser_enrollment` also exercises the source
binding with Chromium and those real helpers via a CDP pipe in a disconnected
network namespace. These are not live HTTPS or host-enrollment acceptance,
nor do they close the artifact, dependency, license, or IPR review gates.
