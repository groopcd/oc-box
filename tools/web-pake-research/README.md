# Offline OCOP v1 verifier research

This checkout-only candidate connects the existing Python `PakeWorker` and
`LocalPakeProfile` to the real `opaque-ke` library over private pipes. The
optional matching browser WASM slot is login-only. Neither half is installed,
selected by the launcher or usable as a web sign-in command. They do not
provision TLS, read an account store, listen or require OpenCode/Podman.
Neither the Python artifacts nor the flat installer use this directory.

## What this worker does

- One child, one login exchange. `OCOP` v1 START imports the 128-byte setup,
  record presence byte and 192-byte registration slot, the four 32-byte
  bindings (credential, client, verifier, context), and 96-byte KE1. The
  response echoes the header and status and includes 320-byte KE2 only on
  success. An absent record requires a zeroed slot and uses the library's
  `None` path. A malformed present record is not a request for a fake record.
- FINISH requires one 64-byte KE3 and stdin EOF before it acknowledges.
  Cancellation at the frame boundary is EOF without FINISH; malformed
  frames, unexpected args and extra data do not yield a successful ACK.
  Even a successful library result cannot produce a success ACK for an
  absent record. Python remains responsible for authorization, deadlines,
  reaping and the existing uncertain-cleanup fence.
- The worker uses the pinned Ristretto255 OPRF and Ristretto255 TripleDH
  (SHA-512), with Argon2id as the **client** KSF (19 MiB, two passes, one
  lane at this pin). System entropy and cryptographic operations are
  provided by the library and its dependencies. The worker does not accept a
  password or export login state, the session key or a native grant. The
  registration/setup and client code in `client_fixture.rs` is a separate
  opt-in build target; it generates only ephemeral synthetic values.

Keep the profile's public binding identical across independently implemented
clients. For the default research profile the account origin is
`https://accounts.ocbox.test:9443` and the app origin is
`https://workspace.ocbox-app.test:9444`. The ID and context hashes include
the versioned domain string and unsigned 16-bit big-endian length prefixes;
the login digest uses the canonical challenge **text**, not the decoded
challenge bytes. The browser nonce, account snapshot and workspace owner
generation are separate host-side authorization bindings. This worker does
not check them or claim timing indistinguishability, runtime isolation,
protection against a same-user debugger or erasure of all memory copies.

## Optional browser login slot

`browser_login.rs` shares the pinned suite with the verifier. It has no
fixture password, identity, network code, form, or enrollment entry point.
`browser_slot.mjs` accepts a caller-verified `WebAssembly.Module` and a private
mutable buffer of 1–1024 UTF-8 bytes from an injected, trusted account-page
workflow. It returns the synchronous `{ke1, finish(bindings), discard()}`
interface of `web_pake_client.mjs` at `eb880f0`. The binding does not load or
approve modules. A trusted caller owns input preparation and always clears
oversized inputs refused before the slot takes ownership.

- A fresh instance takes one password in a bounded slot and emits only KE1.
  At finish it consumes the slot, passing KE2, context, client ID and verifier
  ID to `ClientLogin::finish`. These are supplied or independently derived by
  the trusted adapter; the distinct credential ID is verifier-only. KE3 is
  emitted only after the library verifies the server proof. No session key,
  export key, password or client state is returned to the adapter.
- The only WASM import is `entropy.fill`. The glue requires a secure context,
  uses WebCrypto `getRandomValues` on a checked slice, and has no fallback.
  The build caps linear memory at 64 MiB. The glue refreshes typed-array
  views after calls that may grow memory.
- Normal discard is idempotent and drops Rust's pending slot; the glue also
  clears the caller's accepted buffer and the shared I/O slice. On a WASM or
  entropy trap it drops the instance, never re-enters it, and does not claim
  confirmed private cleanup. This is not guaranteed erasure of all copies.

## Reproduce with existing inputs

Run from the checkout root on x86-64 glibc Linux with Python 3.12+ (including
`tarfile`'s `data` filter), Linux fd-exec/exec-capable sealed memfd and pidfds.
`build` takes an already acquired private archive cache and a nonexistent
output directory outside the cache/checkout. The exact upstream
`opaque-ke 5.0.0-pre.0` crate and original `Cargo.lock`, Rust/Cargo 1.99.0 and
Zig 0.15.2 archives are pinned by SHA-256 in the script. It reconstructs a
private directory source from all 221 crate archives in that lock instead
of trusting unpacked vendor files or downloading anything. The expected
cache layout is the root containing the `.crate` and Zig archives, `rust-files/`
for the Rust component archives and the `cargo-patched/registry/cache/`
directory for the crates, as shown in `build`. No installed compiler or
Cargo config is adopted.

```sh
research=tools/web-pake-research
cache=/path/to/private/input-cache
work=$(mktemp -d)
env -i PATH=/usr/bin:/bin /usr/bin/python3 -I -B "$research/build" \
  "$cache" "$work/first" --with-fixture
env -i PATH=/usr/bin:/bin /usr/bin/python3 -I -B "$research/build" \
  "$cache" "$work/second-relocated-stage"
cmp "$work/first/target/release/ocop-worker" \
    "$work/second-relocated-stage/target/release/ocop-worker"
```

Pass `--with-browser` to each build to include `ocop-browser.wasm` under
`target/wasm32-unknown-unknown/release/`. That target uses the pinned
`wasm32-unknown-unknown` Rust standard-library archive and records its separate
feature graph in `wasm-tree.txt`. It does not enable `rand/sys_rng`.

The script prints artifact digests and keeps the resolved feature graph in
each private output directory. With the verified digests from the build:

```sh
env -i PATH=/usr/bin:/bin /usr/bin/python3 -I -B "$research/check" "$work/first" \
  --worker-sha256 "$worker_digest" --fixture-sha256 "$fixture_digest"
```

Only the verifier under test is loaded into a sealed fd by the existing
Python worker path. The harness also exercises raw framing outside the
adapter and invokes the test fixture from the private build directory. Never
give the harness an arbitrary executable. The in-memory synthetic client
uses the same pinned library, not independent OPAQUE interop. `--frozen`
with a local directory source is offline input resolution, not an OS
network sandbox.

`link` removes exactly two inert Rust `-L` flags when linking the final
binary so this pinned Zig cc does not encode temporary loader search paths.
Do not treat the shim as a general linker wrapper. Independently inspect
the exact artifact's loader, `DT_NEEDED`, RPATH/RUNPATH and ABI floor before
considering distribution. Matching digests locally do not establish ABI
compatibility on older glibc, other architectures, musl or across build hosts.

### Browser conformance scope

Two fresh local builds with `--with-browser` (one without the fixture
target) produced byte-identical native and WASM artifacts. The WASM was
204,179 bytes with SHA-256
`802c138b34fe9b3734240d13397f227f9698724ca9795a993b40d295d81d15ae`;
its only import was `entropy.fill`.

A separate private headless Chromium 159 fixture combined this slot with
the exact `eb880f0` adapter, Python account dispatcher, and the real Rust
verifier through `LocalPakeProfile`. It passed login across two account
names/origin pairs with 37- and 1024-byte synthetic inputs. Wrong
password, changed context/client ID/verifier ID/KE2, out-of-scope account
and cancellation emitted no KE3; a modified KE3 was refused at the
verifier. An initial entropy exception and oversized input sent no POST.
Normal discard and child shutdown were confirmed. The browser and the
opt-in fixture derived public test-vector bytes locally; intercepted POST
bodies contained only the declared PAKE fields.

The fixture fulfilled browser requests via a CDP pipe in a private
network namespace. HTTPS addressing, owner liveness and bootstrap headers
were synthetic; no sockets were listening and no TLS trust, durable
enrollment, app handoff redemption, native pairing or workspace admission
was tested. This is same-library functional conformance, not a browser
performance, independent interoperability, provenance or release result.

## Not ready to enable

1. **Initialization, enrollment and reset are missing.** Design a separate
   versioned native helper contract for setup generation (128 bytes, stored
   only in encrypted host state), blinded registration request/response
   (32/64 bytes) and validation/canonicalization of a 192-byte registration
   upload. The fixture's client-internal setup and registration is not this
   service. The browser must use the exact same immutable account IDs,
   verifier origin, suite and KSF as login. Upload validity alone is not
   authorization. Require a host-authorized, short-lived one-use enrollment
   or reset bound to the browser, exact account, verified workspace scope
   and expected setup/record generations. Atomically recheck on write, close
   or fence stale admissions/native grants, and explicitly design setup
   retirement/re-enrollment and rollback handling. Do not silently migrate
   incompatible registration bytes; loss of both setup and records can
   enable offline guessing.
2. **The prerelease is an experiment, not a dependency decision.** The
   preserved upstream 4.0.1 lock uses `crypto-bigint 0.5.5` (the
   [RustSec advisory](https://rustsec.org/advisories/RUSTSEC-2026-0387.html)
   lists `>=0.7.6` as patched). The research lock uses `0.7.11` and
   `voprf 0.6.0-pre.2`. Verify a stable patched candidate's exact active
   native, WASM and build graphs and investigate the relevant advisory
   surface for the selected profile and targets. A separately named fork
   or a version substitution is not upstream stability, record or wire
   compatibility, or clearance. Revalidate upstream vectors, independent
   interop, negative tests and cryptographic/serialization changes on any
   change to pin or profile.
3. **No production artifact or host acceptance.** Independently review
   exact source and build provenance, advisories, licenses/notices and
   applicable IPR; choose the supported ABI targets, versioned asset
   manifest and distribution/update ownership. Repeat with the actual
   reviewed browser bundle, durable store and trusted HTTPS origins on an
   exact integrated host candidate. The existing
   [web runtime plan](../../docs/plans/web-runtime.md) explains the separate
   admission, native pairing and release gates. Nothing here changes the
   native one-use-link route or authorizes distribution.
