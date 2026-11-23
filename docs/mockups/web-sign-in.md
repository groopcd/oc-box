# Web sign-in design

This is a design for a future launcher mode. The offline mockup shows the
browser experience; it never performs authentication. Experimental runtime
layout and TLS file preparation are documented in the [CLI reference](../cli.md#web--browser-interface).
Named sign-in, per-browser logout and network listening remain disabled;
operator-supplied TLS files do not enable those modes.

| Mode | Proposed CLI | Link shown in the host terminal | Browser experience |
| --- | --- | --- | --- |
| One-use link (default) | `oc-box web` | Localhost only (for example `http://127.0.0.1:55061/auth/connect/<one-use-code>`). No non-loopback bind allowed. | Native pairing opens the workspace as **Workspace guest**. The link proves access, not the identity of the person opening it. |
| Password, local | `oc-box web --auth password` | HTTPS localhost sign-in URL with no grant, password or pairing code. | A centered form asks for a web username and password. The account menu shows the authenticated name. |
| Password, network | `oc-box web --auth password --bind 0.0.0.0` | Explicit opt-in on all IPv4 interfaces. Advertise a reachable HTTPS URL, such as `https://workspace.example.com/auth/oc-box-login`, not `0.0.0.0`. | The same named sign-in; no one-use link is exposed to the network. |

Both modes put **Log out** in the top-left account dropdown. Live logout
must revoke only that browser session, including its active connections, not
stop the workspace or sign out other users. A one-use-link guest needs a fresh
link to return; a named user can sign in again.

## Certificate setup

The commands in this section are proposals and are not exposed by the installed
guest-only launcher.

TLS is configured per web origin, not per vault user. Creating or resetting an
account does not issue certificates, modify browser trust, or enable a listener.

| Setup | Proposed operator flow | Client trust |
| --- | --- | --- |
| Localhost | `oc-box web tls setup localhost` uses optional `mkcert` to create managed TLS files. Check existing operator-supplied files separately. Any trust-store change requires the explicit `--install-trust` option and typed host approval. | The current opt-in supports a validated user NSS database only. Verify actual profiles; generation/import does not prove Chrome, Firefox, or Brave trusts the issuer. Keep this profile loopback-only. |
| Private LAN | Use a private CA for an internal name or private IP, **or** a Let's Encrypt certificate for a hostname under a domain you control. Verify the endpoint before enabling the network listener. | Private CA: install only its **public certificate** on client devices; never distribute the CA private key. Let's Encrypt: normal browser trust when clients use the certified hostname. |
| Publicly trusted | Supply an existing certificate and key, or provision and renew them with an external ACME client such as Certbot or Caddy. Public certificate trust does not require making oc-box publicly reachable. | Normal public-CA trust; users visit the certified hostname. Keep the listener explicitly enabled and scoped to the desired network. |

For example, issue a Let's Encrypt certificate for `workspace.example.com`
using [DNS-01 validation](https://letsencrypt.org/docs/challenge-types/#dns-01-challenge).
The challenge's TXT record must be visible in public DNS, but your LAN DNS
can resolve the hostname to a private IP (split DNS). No inbound internet
port is required for issuance or renewal; keep the listener restricted to
the intended network. Clients must use the certified hostname, not the private
IP directly. Let's Encrypt cannot certify a private IP or a LAN-only name
such as `workspace.local`. Keep DNS credentials with the external ACME
client, preferably scoped to challenge records, not in the workspace.
Public certificate issuance can disclose the hostname in Certificate
Transparency logs; use a private CA if that matters.

The localhost command should check prerequisites, validate the hostnames,
certificate dates and key match, and explain how to verify trust in a real
browser. Don't silently install tools, install a trust root, or claim all
profiles are trusted. Use an existing external tool rather than writing a CA
or ACME client. Keep keys private, outside workspaces and backups of
workspace data, and refuse to start named sign-in if the TLS configuration
is missing or invalid. If TLS terminates outside oc-box, the live design must
verify the origin and keep the upstream path private; the wire protection and
password-hiding requirements still apply. The first implementation should
accept operator-supplied TLS files before adding automated provisioning.

### Read-only checks

- **`oc-box doctor --feature web-tls-local`**: report whether the
  host has the optional mkcert helper and any trust tools it needs on that
  platform (for example, `certutil` for NSS stores on Linux). A missing helper
  blocks the mkcert setup route, not ordinary guest web, supplied certificates,
  or launching with an already valid local certificate. Do not turn mkcert
  into a mandatory dependency in a default doctor run.
- **`oc-box web tls check localhost`**: on-demand, read-only check
  of the active localhost TLS configuration. Report missing or unreadable
  files, certificate validity, name coverage, and certificate/key match
  separately from helper availability. Report browser trust as unverified
  unless checked in the actual browser profile. No trust-store changes,
  package installs, certificate issuance, or workspace startup.
- **`oc-box web tls setup localhost`** runs the helper preflight before any
  setup action. Only an explicitly approved setup can issue a certificate or
  change trust stores.

These options belong to `oc-box web` and its feature-scoped host diagnostics.
Do not add web settings or setup controls to the local Textual TUI. The browser
preview can explain the setup choices and show the host commands, but a
pre-login page must not be able to issue certificates, modify trust, or run a
host setup command.

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

## Named-mode implementation gates

The internal encrypted account store, bounded PAKE exchange state machine and
in-memory browser admission registry are not connected to a listener. Their
tests use synthetic inputs and do not establish real authentication. Do not
enable named mode until:

- The exact OPAQUE verifier and browser build, dependencies, provenance and
  distribution terms have been independently reviewed. A registry signature
  or earlier audit alone does not establish that the current WebAssembly
  artifact is safe to ship. Resolve relevant upstream advisories; require
  reproducible offline packaging and browser/native interoperability evidence.
- Credential entry and enrollment use a dedicated, host-controlled HTTPS
  origin that never serves native workspace content. Keep workspace and
  account origin cookies and trust separate. A difference in port alone is
  not cookie isolation. Use a short-lived, single-use, browser-bound handoff
  for an explicitly authorized workspace and verified owner. Never allow an
  account or same-origin host API to be chosen by workspace content.
- Every named app request and upgrade passes the new admission boundary,
  including subsequent requests on reused connections. Preserve native
  authorization as a separate requirement; verify the pinned native
  HTTPS pairing/cookie contract with the actual browser and ensure logout
  revokes the browser grant and ends its streams without claiming native
  per-cookie revocation. No raw-password, guest-link, or HTTP fallback.
- A separate explicit authorization design exists before exposing host-wide
  manager data and actions. The paired workspace browser is not that design.
