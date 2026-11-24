// SPDX-License-Identifier: GPL-3.0-or-later
// Synthetic CDP fixture only: no form, saved state or app handoff.
import { createLocalPakeClient } from "/adapter.mjs";
import { openLoginAttempt } from "/slot.mjs";

globalThis.exercise = async ({ accountOrigin, workspaceOrigin, username, size, mode }) => {
  // The Python runner pins the bytes before ever serving this private URL.
  const module = await WebAssembly.compileStreaming(fetch("/client.wasm"));
  const controller = new AbortController();
  globalThis.cancelFixture = () => controller.abort();
  const result = {
    opened: 0, reads: 0, entropy: 0, finishes: 0, cleanup: [],
    cleared: true, immutable: true, reuseRefused: true, accessorCalled: false,
  };
  const refusal = "Named sign-in refused.";
  let vector, block, phase = mode;
  const realRandom = crypto.getRandomValues;
  crypto.getRandomValues = function (output) {
    result.entropy++;
    // At the first entropy call, JS should already have wiped the input view.
    result.cleared &&= vector.every(byte => byte === 0);
    if (phase === "entropy-trap") throw new Error("synthetic entropy trap");
    return realRandom.call(this, output);
  };
  const config = {
    accountOrigin, workspaceOrigin,
    openAttempt() {
      result.opened++;
      const attempt = openLoginAttempt(phase === "bad-module" ? null : module);
      return {
        start(readPassword) {
          try { return attempt.start(readPassword); }
          finally {
            if (vector && phase !== "over-scrub-limit") {
              result.cleared &&= vector.every(byte => byte === 0) &&
                block[0] === 255 && block.at(-1) === 255;
            }
          }
        },
        finish(input) {
          result.finishes++;
          const field = {
            "changed-client": "clientIdentifier", "changed-verifier": "verifierIdentifier",
            "changed-context": "context", "changed-ke2": "ke2",
          }[phase];
          const bindings = { ...input };
          if (field) {
            bindings[field] = bindings[field].slice();
            bindings[field][0] ^= 1;
          }
          if (phase === "binding-accessor") {
            Object.defineProperty(bindings, "context", {
              get() { result.accessorCalled = true; return input.context; },
            });
          }
          const snapshots = Object.values(input).map(bytes => bytes.slice());
          try {
            const proof = attempt.finish(Object.freeze(bindings));
            if (phase === "changed-proof") proof[0] ^= 1;
            return proof;
          } finally {
            result.immutable &&= Object.values(input).every(
              (bytes, i) => bytes.every((byte, j) => byte === snapshots[i][j]));
            try {
              attempt.finish(input);
              result.reuseRefused = false;
            } catch (error) { result.reuseRefused &&= error.message === refusal; }
          }
        },
        discard() {
          const first = attempt.discard();
          result.cleanup.push(first === attempt.discard() ? first : null);
          try {
            attempt.start(() => { result.reuseRefused = false; });
            result.reuseRefused = false;
          } catch (error) { result.reuseRefused &&= error.message === refusal; }
          return first;
        },
      };
    },
  };
  const auth = createLocalPakeClient(config);
  // Mutating the caller's config cannot redirect the exchange or its binding.
  config.accountOrigin = config.workspaceOrigin = "https://invalid.test:4444";
  config.openAttempt = () => { throw new Error("mutated factory"); };
  const input = {
    username, signal: controller.signal,
    readPassword() {
      result.reads++;
      if (phase === "reader-throws") throw new Error("synthetic reader refusal");
      // Public test-vector bytes are derived only here and in the seed fixture.
      const length = phase === "over-scrub-limit" ? 65_537 : size + (phase === "oversized");
      block = new Uint8Array(length + 2).fill(255);
      vector = block.subarray(1, -1);
      for (let i = 0; i < vector.length; i++) vector[i] = 65 + i % 26;
      if (phase === "wrong") vector[0] ^= 1;
      return vector;
    },
  };
  const tryExchange = async () => {
    try { return await auth.exchange(input); }
    catch (error) {
      if (error.message !== refusal) throw new Error("non-generic refusal");
      return null;
    }
  };
  try {
    if (mode === "pre-aborted") cancelFixture();
    let ticket = await tryExchange();
    if (["entropy-trap", "over-scrub-limit", "bad-module", "reader-throws", "oversized"]
        .includes(mode)) {
      // A retry is only possible after confirmed cleanup. It always uses the
      // verified module and in-range synthetic vector.
      if (ticket !== null) throw new Error("local refusal produced a ticket");
      vector?.fill(0); // Fixture cleanup after deliberately violating the reader contract.
      phase = "valid";
      ticket = await tryExchange();
    }
    result.ticket = ticket !== null;
    if (ticket) {
      const opens = result.opened;
      if (await tryExchange() !== null || result.opened !== opens) {
        throw new Error("pending ticket did not own the controller");
      }
      if (mode === "abort-ticket") cancelFixture();
      if (mode === "hide-ticket") dispatchEvent(new PageTransitionEvent("pagehide"));
      if (mode === "expire-ticket") await new Promise(resolve => setTimeout(resolve, 5200));
      result.taken = typeof ticket.take() === "string";
      result.singleUse = ticket.take() === null;
    }
    return result;
  } finally {
    crypto.getRandomValues = realRandom;
    vector?.fill(0);
    delete globalThis.cancelFixture;
  }
};
