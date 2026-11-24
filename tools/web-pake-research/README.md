# Offline OCOP v1 verifier research

This checkout-only candidate connects the existing Python `PakeWorker` and
`LocalPakeProfile` to the real `opaque-ke` library over private pipes. It is
not installed, selected by the launcher or usable as a web sign-in command.
It does not build the browser client, provision TLS, read an account store,
listen or require OpenCode/Podman. Neither the published Python artifacts nor
the flat installer use this directory.

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
