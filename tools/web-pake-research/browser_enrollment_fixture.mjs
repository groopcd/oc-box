// SPDX-License-Identifier: GPL-3.0-or-later
// Synthetic CDP fixture only: never load this from an installed account page.
import { createLocalEnrollmentClient } from "/enrollment-client.mjs";
import { openEnrollmentAttempt } from "/enrollment-slot.mjs";

globalThis.exerciseEnrollment = async config => {
  const { mode, size, accountOrigin, workspaceOrigin, username } = config;
  const url = mode === "login-module" ? "/old-login.wasm" : "/enrollment.wasm";
  const module = await WebAssembly.compileStreaming(fetch(url));
  const scope = new AbortController();
  globalThis.cancelEnrollmentFixture = () => scope.abort();
  if (mode === "pre-aborted") scope.abort();
  const originalEntropy = crypto.getRandomValues;
  let entropy = 0;
  crypto.getRandomValues = function (bytes) {
    entropy++;
    if (mode === "entropy-trap") throw new Error("synthetic entropy refusal");
    return originalEntropy.call(this, bytes);
  };
  let opens = 0, reads = 0, registers = 0, proofs = 0, password;
  const cleanup = [], outputs = [];
  function openAttempt() {
    opens++;
    const handle = openEnrollmentAttempt(module);
    return Object.freeze({
      start(read) {
        const request = handle.start(read);
        outputs.push(request);
        return request;
      },
      finishRegistration(values) {
        registers++;
        if (mode === "changed-verifier") {
          values.verifierIdentifier[0] ^= 1;
        }
        const upload = handle.finishRegistration(values);
        outputs.push(upload.registration, upload.ke1);
        return upload;
      },
      finishProof(values) {
        proofs++;
        const proof = handle.finishProof(values);
        if (mode === "tampered-proof") proof[0] ^= 1;
        outputs.push(proof);
        return proof;
      },
      discard() {
        const state = handle.discard();
        if (handle.discard() !== state) throw new Error("synthetic teardown inconsistency");
        cleanup.push(state);
        return state;
      },
    });
  }
  try {
    const client = createLocalEnrollmentClient({
      accountOrigin, workspaceOrigin, openAttempt,
      approvedUsername: mode === "different-account" ? "synthetic-other" : username,
    });
    const input = {
      signal: scope.signal,
      readPassword() {
        reads++;
        // Public synthetic test pattern derived locally; never sent as input
        // to the fixture via CDP or written by the fixture.
        password = Uint8Array.from({ length: mode === "oversized" ? 1025 : size },
                                  (_, index) => 65 + index % 26);
        return password;
      },
    };
    async function run() {
      try { return (await client.enroll(input)) === true; }
      catch (error) {
        if (error.message !== "Named enrollment refused.") throw new Error("unexpected browser error");
        return false;
      }
    }
    const verified = await run(), reused = await run();
    return {
      verified, reused, opens, reads, registers, proofs, cleanup, entropy,
      cleared: (!password || password.every(byte => byte === 0)) &&
        outputs.every(bytes => bytes.every(byte => byte === 0)),
    };
  } finally {
    crypto.getRandomValues = originalEntropy;
    password?.fill(0);
    delete globalThis.cancelEnrollmentFixture;
  }
};
