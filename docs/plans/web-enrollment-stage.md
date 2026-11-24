# Internal staged named enrollment

This is the host coordinator contract only. It does not expose a route,
install an artifact, select an account, change the store, or grant access.

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

Only a verified KE3 followed by a confirmed drain of both workers can make
the result available. Only the trusted host calls `take_for_host()`, once;
the returned review, registration and `current()` guard never go to HTTP.
Before persisting, the host still needs its local confirmation, owner
exclusion/revocation and `commit_registration(..., current=result.current)`.
The guard checks approval/claim liveness; it does not replace the store's
locked comparison against the original ciphertext and review. Do not close
or retire the approval before the guarded write; successful proof already
closes the native workers without invalidating the host result.

The owner calls `expire()` periodically and on bootstrap retirement. This
fences stale work and initiates bounded worker teardown. `close()` permanently
retires the approval and returns whether both workers and any in-flight call
have drained. Keep the outer teardown fence when it returns `False`.
`EnrollmentCleanupError` and `cleanup_failed` indicate uncertainty, and
the latter is sticky. A subsequent explicit `close()` may verify cleanup of
the **same** workers without reopening approval or replaying a round.

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
the existing pinned OCOR/OCOP binaries. It passes synthetic proof, altered
KE3, wrong context or verifier, wrong browser, replay, and bootstrap
retirement tests without a listener or account store. Separate Python tests
exercise the encrypted store's stale-review refusal. These are not live HTTPS
or host-enrollment acceptance, nor do they close the artifact, dependency,
license, or IPR review gates.
