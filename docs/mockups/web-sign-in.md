# Web sign-in design

This is a design for a future launcher mode. The offline mockup shows the
browser experience; it does not implement production authentication or change
`oc-box web`.

| Mode | Proposed CLI | Link shown in the host terminal | Browser experience |
| --- | --- | --- | --- |
| One-use link (default) | `oc-box web` | Native `/auth/connect/<one-use-code>` URL, as today. | Native pairing opens the workspace without a password form. |
| Password | `oc-box web --auth password` | Plain loopback URL, for example `http://127.0.0.1:55061/auth/oc-box-login`. It contains no grant, password or pairing code. | A centered workspace-branded form asks for a dedicated web password. Only successful sign-in opens the workspace. |

## Credential boundary

- Keep the dedicated web credential scoped to web sign-in and separate from
  the vault master passphrase, provider credentials and the native service
  password. Configure it explicitly via the host vault. Do not forward it
  into provider, agent, shell or workspace environments: it is not an ordinary
  vault environment entry. Vault unlock stays in the existing masked host
  prompt; the browser must never ask for the vault passphrase.
- Keep password mode explicitly selected, loopback-only, and bound to the
  intended private workspace service and relay lifetime. An unavailable vault
  credential or unsupported native handshake must refuse password mode,
  not fall back to printing a pairing grant.
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

The offline preview’s known test password is not a security boundary. It
demonstrates the centered form, masked entry, show/hide and retry states only.
