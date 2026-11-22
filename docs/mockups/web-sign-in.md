# Web sign-in design

This is a design for a future launcher mode. The offline mockup shows the
browser experience; it does not implement production authentication or change
`oc-box web`.

| Mode | Proposed CLI | Link shown in the host terminal | Browser experience |
| --- | --- | --- | --- |
| One-use link (default) | `oc-box web` | Localhost only (for example `http://127.0.0.1:55061/auth/connect/<one-use-code>`). No non-loopback bind allowed. | Native pairing opens the workspace as **Workspace guest**. The link proves access, not the identity of the person opening it. |
| Password, local | `oc-box web --auth password` | Localhost sign-in URL with no grant, password or pairing code. | A centered form asks for a web username and password. The account menu shows the authenticated name. |
| Password, network | `oc-box web --auth password --bind 0.0.0.0` | Explicit opt-in on all IPv4 interfaces. Advertise a reachable HTTPS URL, such as `https://workspace.example.com/auth/oc-box-login`, not `0.0.0.0`. | The same named sign-in; no one-use link is exposed to the network. |

Both modes put **Log out** in the top-left account dropdown. Live logout
must revoke only that browser session, including its active connections, not
stop the workspace or sign out other users. A one-use-link guest needs a fresh
link to return; a named user can sign in again.

## Credential boundary

- Support multiple named users: store one salted password verifier and
  explicit workspace access scope per username, not reusable plaintext
  passwords. Keep these records scoped to web sign-in and separate from
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
- Before enabling the network bind, require HTTPS at the exposed endpoint
  and validate its public origin and certificate configuration. Refuse
  non-loopback cleartext; cookies and login requests must not travel over
  unencrypted network HTTP. `0.0.0.0` is not an authorization rule: firewall
  and network reachability are separate. Default one-use-link mode stays
  loopback-only regardless of other settings.
- Before implementation, verify the native pairing/authentication contract:
  the login page must not grant pre-login access to the app, API, WebSocket,
  native pairing route, or attachment routes. Only a successful login can
  obtain a fresh native pairing grant; keep it out of the printed URL and logs.
  Preserve native cookie and Origin enforcement, with bounded attempts and
  request handling. Do not persist browser form values or place secrets in
  URLs, logs, telemetry, argv, environment, or temporary files.
- Password sign-in authorizes the same workspace access class as ordinary
  native pairing, not the separate host manager, host catalogs, terminal
  administration or backup operations.

The offline preview’s two hard-coded accounts are not a security boundary.
They demonstrate named sign-in, the guest label, and both logout flows.
