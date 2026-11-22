# Web sign-in design

This is a design for a future launcher mode. The offline mockup shows the
browser experience; it does not implement production authentication or change
`oc-box web`.

| Mode | Proposed CLI | Link shown in the host terminal | Browser experience |
| --- | --- | --- | --- |
| One-use link (default) | `oc-box web` | Localhost only (for example `http://127.0.0.1:55061/auth/connect/<one-use-code>`). No non-loopback bind allowed. | Native pairing opens the workspace as **Workspace guest**. The link proves access, not the identity of the person opening it. |
| Password, local | `oc-box web --auth password` | HTTPS localhost sign-in URL with no grant, password or pairing code. | A centered form asks for a web username and password. The account menu shows the authenticated name. |
| Password, network | `oc-box web --auth password --bind 0.0.0.0` | Explicit opt-in on all IPv4 interfaces. Advertise a reachable HTTPS URL, such as `https://workspace.example.com/auth/oc-box-login`, not `0.0.0.0`. | The same named sign-in; no one-use link is exposed to the network. |

Both modes put **Log out** in the top-left account dropdown. Live logout
must revoke only that browser session, including its active connections, not
stop the workspace or sign out other users. A one-use-link guest needs a fresh
link to return; a named user can sign in again.

## Credential boundary

- Support multiple named users: keep one protocol registration record and
  explicit workspace access scope per username, never a plaintext or
  reversibly encrypted password. Keep these records scoped to web sign-in and separate from
  the vault master passphrase, provider credentials and the native service
  password. Configure it explicitly via the host vault. Do not forward it
  into provider, agent, shell or workspace environments: it is not an ordinary
  vault environment entry. Vault unlock stays in the existing masked host
  prompt; the browser must never ask for the vault passphrase.
- Keep both modes bound to the intended private workspace service and relay
  lifetime. An unavailable vault credential or unsupported native handshake
  must refuse password mode, not fall back to printing a pairing grant.
  The all-interface bind affects only the host web listener. The container
  service remains private; do not publish its port.
- Require HTTPS in **password mode even on localhost**. Serve the sign-in
  page and all authentication messages only over TLS. Use a trusted certificate
  matching the advertised hostname; fail closed if TLS is unavailable or
  misconfigured, without an HTTP fallback or instructions to bypass a browser
  certificate warning.
- **The browser never sends the password itself, including during
  enrollment.** Evaluate a reviewed implementation of an augmented
  password-authenticated key exchange such as
  [OPAQUE (RFC 9807)](https://www.rfc-editor.org/rfc/rfc9807.html).
  Send only protocol messages, not a raw password, a reusable client-side hash,
  or a password encrypted for the server to decrypt. No custom cryptography
  or fallback to a password POST. Review the enrollment/reset flow, the
  browser implementation and the protection of server-side records and keys
  before enabling named sign-in.
- The typed password necessarily exists briefly in the browser while entering
  and processing it. Mask entry, clear the field after each attempt, and do
  not intentionally persist it or claim guaranteed erasure. This does not
  protect against a compromised browser or malicious sign-in JavaScript served
  to the browser. Neither a TLS terminator nor the verifier should receive a
  password it can read. Do not log protocol bodies or forward the password
  to native OpenCode. `0.0.0.0` is not an
  authorization rule: firewall and network reachability are separate. Default
  one-use-link mode stays loopback-only regardless of other settings.
- Before implementation, verify the native pairing/authentication contract:
  the login page must not grant pre-login access to the app, API, WebSocket,
  native pairing route, or attachment routes. Only a successful login can
  obtain a fresh native pairing grant; keep it out of the printed URL and logs.
  Preserve native cookie and Origin enforcement. Issue a fresh session on
  sign-in using an opaque cookie with `Secure`, `HttpOnly`, and a restrictive
  `SameSite` policy consistent with the native flow. Bound request bodies,
  login attempts and verifier work; use a generic rejection for bad usernames
  or passwords and protect sign-in/logout against cross-origin requests. Do
  not persist browser form values or place secrets in URLs, logs, telemetry,
  argv, environment, or temporary files.
- Password sign-in authorizes the same workspace access class as ordinary
  native pairing, not the separate host manager, host catalogs, terminal
  administration or backup operations.

The offline preview’s two hard-coded accounts are not a security boundary.
They demonstrate named sign-in, the guest label, and both logout flows.
