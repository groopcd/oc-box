# Original owning guest proof (unserved)

An internal opt-in on `workspace_service.Owner` exercises the producer side of
one-use Studio's original workspace proof. There is no launcher or browser
caller. Legacy `lease`, named-only `named-lease`, native pairing and legacy
control payloads retain their wire formats. The proof does not enable named
mode or replace runtime-safeguard admission. The current proof profile refuses
guarded and named owners, devices, custom Podman arguments, a present controller
(including stale socket paths), and anything other than exact native 2.4.1.

Under the original exclusive workspace lifecycle lock, the host checks the
path and kernel flock, the unchanged production stop token, the exact workspace
registration and project identity. The existing strict running-container
inspection verifies the label, project and named-home bindings, no port
publication, the full container ID and its actual `StartedAt` generation. Once
captured, inspection and the guest proof worker use that immutable ID; they
never fall back to the mutable name. Before spawning, the host also holds
short workspace admission. A stop published during admission can still race
an already-admitted spawn; readiness rechecks the token before publish.

The private guest worker launches OpenCode using the normal V2 worker and
requires authenticated `/api/info` on that worker's own endpoint. The exact
native PID in the reply must match the directly launched child. The private
`guest-proof-lease` frame carries endpoint credentials and the version/PID/URL
projection, not an HTTP grant. At runtime, only the original host-held proof
can return the credential-free identity. Every observation rechecks the
original lock, registration, container generation and child; an observed
failure or interrupt is sticky. It does not attest a legacy `AttachedControl`,
whose counted stream is still not independently bound to a native owner.

Guest worker normal exit is only acknowledged after input EOF was observed
with the original native child still alive, and that child then exited cleanly
without force. This concerns the native owner worker, **not** gateway HTTP or
WebSocket drain. The ordinary workspace shutdown implementation is unchanged.

## Before any route is enabled

- Bind the original listener, the native proof and the single-use cookie
  verifier to the same owning gateway; recheck the exact run before any
  sensitive native request or delivery.
- Register every accepted stream, worker and managed relay before it can
  dispatch. On close, revoke new work, wake and retire the original tracked
  workers, and require an affirmative drain result before history sync or
  releasing the owner. An empty child set, a timeout or the stop marker alone
  does not prove shutdown. Do not make this seam an implicit fallback.
- Attach-only launch still needs a separately reviewed generation proof on its
  original counted stream and must not restart if that owner disappears. Do
  not treat the same-UID peer, URL or copied connection fields as proof.
- The running-container and lock checks cannot exclude malicious same-user
  manipulation or external Podman changes between observations. The first
  supported profile also needs a real installed Podman run: the local smoke
  proves the unmodified ELF readiness, not that every packaged CLI entrypoint
  execs the ELF in place or that the default gateway drains.

`web_studio_guest_binding.bind_unserved` joins the exact owning proof to the
actual Gateway reservation, with the original owner connection and pinned
container selection. It is scoped to a one-shot, non-listening probe; it
does not mint a cookie, register a browser grant, or expose a protected API.

`tests/smoke_web_studio_guest_owner.py` uses the exact unmodified 2.4.1 ELF
with a synthetic project and substituted Podman inspect/exec. It tests the
original native child, the unserved listener binding, revocation on a real
stop token change, and EOF/reap.
It serves no guest routes, performs no inference and does not establish host
acceptance.
