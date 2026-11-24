# Managed Studio guest native-cookie verifier (unserved)

`web_studio_guest_native` implements the cookie-only native verification seam
of [Studio guest admission](web-studio-guest.md). It serves no HTTP route,
mints no native cookie or link, and does not change the ordinary HTTP gateway,
`GuestAdmission`, or the named HTTPS policy.

## Exact trusted handoff

The adapter must already hold the original verified listener and owner
lease. From that proof, construct `GuestNativeScope(binding, endpoint,
native_pid, version)`. Use the original **`GuestBinding` object**, actual
2.4.1 release, exact private loopback endpoint in the verified container
namespace, and the original **native process PID in that namespace**.
These are not request parameters; neither constructing a scope nor observing
an HTTP identity attests the owner. `Gateway.url`, an `AttachedControl`,
the same-user peer PID, and the optional UI generation do not supply this
proof. Never replace or reacquire the owner/listener on refusal.

At registration, create one `ManagedGuestCookieVerifier(box, scope,
observe_gateway=...)` for that specific exchange. Install cancellation of
the exact downstream exchange **and** `attempt.cancel()` in the callback
passed to `GuestAdmission.confirm`. The admission registry keeps one
fixed trusted `verify_native` dispatcher with its existing signature:

```python
verify_native(binding, *, use, headers, authority, deadline)
```

Dispatch synchronously to that exchange's exact attempt, for example through
a private `ContextVar` with default `None`, set and reset in a `try/finally`
around `confirm`. Do not keep a shared "current attempt", retry an attempt,
or accept an attempt or observer from a request. The `use` must be the one
the original registry actually registered. The observer must recheck the
same listener and held owner with the given use and absolute deadline,
return the exact binding object or `None`, and permanently fence owner loss
or uncertainty. The wrapper does not replace that verification or monitor.

For any registered result, the adapter must retire the downstream and call
`finish(use, confirmed=...)` using **both** actual downstream evidence and
`attempt.cleanup_confirmed`. This applies to denial too. On unconfirmed
cleanup, keep the exact registered use and original lease pinned; do not
replay, repair, launch again, or report a completed shutdown. Overall
gateway shutdown still requires independent positive retirement of that
listener and all accepted workers before releasing the lease.

## Wire and process evidence

- Accept only the vetted header block from `GuestAdmission`: the fixed
  `GET /api/info HTTP/1.1` request line and exact `host`, `origin`, and
  `cookie` fields, with the original canonical HTTP loopback authority and
  no appended fields or body. Cookie data is opaque to the verifier, not
  proof by format. It is passed only on the private registered stream,
  never retained in a handle, passed in argv/env, or logged.
- The attempt uses **at most two** sequential managed relays to the same
  immutable container and native endpoint. First it sends the identical
  fixed request **without any Cookie or Authorization** and requires the
  bounded, exact native 401 before sending the browser's Cookie in the
  second. A public 200 is a refusal even if its JSON matches the identity.
  The native 401 advertises a Basic challenge; the verifier never answers
  it. No alternate routes, Basic/key fallback, mint, redirect, or retry.
- The second exchange must produce the bounded native HTTP/1.1 200 JSON
  identity with the exact native version, PID and private endpoint, and
  the exact loopback origin in the native response. Duplicate fields or
  JSON keys, out-of-profile response fields, body framing mismatches,
  trailing bytes and unsupported native identities refuse. No native
  response content goes to the browser or logs.
- Each relay registers the exact providerless managed exec and socket
  before launch. The fixed connector rechecks the owner and cancellation
  through startup; once launch has been admitted, the OS spawn can race
  cancellation, but is already registered and cannot send the Cookie
  without a fresh check. The total request budget is at most six seconds,
  limited further by the original use/deadline. Cleanup has the managed
  relay's separate bounded grace. Only genuine relay EOF plus a real clean
  `waitpid` exit and retired host socket count after a launch attempt.
  Local shutdown, a missing process handle, or a forced/nonzero exit is
  not proof; a prelaunch refusal with no attempted process needs no EOF.
  The exact binding is returned only after both positive retirement
  checks and a final owner check.

## Isolated native evidence

The opt-in fixture
`python3 -B tests/smoke_web_studio_guest_native.py --opencode /path/to/opencode`
was verified with the unmodified `@opencode/cli-linux-x64` 2.4.1 executable
(SHA-256 `ec93043f18f8a6b4d8e71ec9260e292ba6f91b66b142782135d40f0b58b0c152`),
matched locally to the retained npm archive and recorded SHA-512 SRI.
It uses a fresh synthetic project and private native state, no provider
credentials, and no inference. It mints the real native one-use pairing
cookie using the synthetic fixture's existing setup auth only, verifies
pairing replay refusal, and checks that the actual `/api/info` rejects
missing and invalid cookies, a cookie minted for a different loopback
authority, and a valid cookie with a foreign Origin. It demonstrates that
the actual native root returns a public 200 but is not a verifier target.
The offline managed relay matrix additionally rejects a response to
cookie-free `/api/info` that returns 200 with a perfectly formed identity,
before ever forwarding the cookie.

In the native fixture the real admission and verifier ran through the exact
providerless relay body, replacing only the outer exec with local execution.
The genuine cookie admitted, the invalid and foreign cookies refused, all
six managed children had already been reaped with exit zero, admission
drained, and the freshly started native server shut down without a force
kill. This validates the exact native auth exchanges and local managed
transport, not original gateway/listener owner attestation, native logout
continuity, browser composition, or Podman/host acceptance. None of those
remaining prerequisites is waived.
