// SPDX-License-Identifier: GPL-3.0-or-later
// Local glue only. The trusted caller supplies the verified module and bytes.

const IO = 1024;
const REFUSAL = "Named sign-in refused.";

function require(ok) {
  if (!ok) throw new Error(REFUSAL);
}

/**
 * Synchronous createSession() binding for web_pake_client.mjs (eb880f0).
 * Consumes and clears the caller's private, mutable UTF-8 byte buffer (1–1024 bytes).
 * The caller must clear rejected oversized inputs and never load arbitrary modules.
 */
export function createLoginSlot(module, password) {
  let wasm, offset, trapped = false, consumed = false, closed;
  const owned = password instanceof Uint8Array && password.buffer instanceof ArrayBuffer &&
    password.byteLength <= IO;
  const io = () => {
    const buffer = wasm?.memory?.buffer;
    require(buffer && Number.isInteger(offset) && offset > 0 && offset <= buffer.byteLength - IO);
    return new Uint8Array(buffer, offset, IO);
  };
  const scrub = () => {
    try { io().fill(0); return true; } catch { return false; }
  };
  const invoke = (name, ...args) => {
    try {
      return wasm[name](...args);
    } catch {
      trapped = true;
      throw new Error(REFUSAL);
    }
  };
  const discard = () => {
    if (closed !== undefined) return closed;
    let confirmed = false;
    try {
      // An entropy trap may have left Rust's borrow/state mid-operation.
      // Never re-enter that instance or claim its private cleanup succeeded.
      if (wasm && !trapped) confirmed = invoke("discard") === 1;
    } catch {
      confirmed = false;
    } finally {
      const scrubbed = scrub();
      wasm = undefined;
      closed = confirmed && scrubbed;
    }
    return closed;
  };
  const readResult = (operation, expected, ...args) => {
    try {
      require(invoke(operation, ...args) === expected);
      // Argon2 may grow memory, detaching previous views.
      return io().slice(0, expected);
    } finally {
      if (!scrub()) trapped = true;
    }
  };
  try {
    require(owned && password.byteLength >= 1 &&
      globalThis.isSecureContext === true && typeof globalThis.crypto?.getRandomValues === "function" &&
      module instanceof WebAssembly.Module);
    const imports = WebAssembly.Module.imports(module);
    require(imports.length === 1 && imports[0].module === "entropy" &&
      imports[0].name === "fill" && imports[0].kind === "function");
    const random = globalThis.crypto.getRandomValues.bind(globalThis.crypto);
    wasm = new WebAssembly.Instance(module, { entropy: {
      fill(ptr, length) {
        const buffer = wasm?.memory?.buffer;
        require(buffer && Number.isInteger(ptr) && ptr >= 0 &&
          Number.isInteger(length) && length >= 0 && length <= 65_536 &&
          ptr <= buffer.byteLength - length);
        random(new Uint8Array(buffer, ptr, length));
      },
    } }).exports;
    require(wasm.memory instanceof WebAssembly.Memory && invoke("io_size") === IO);
    offset = invoke("io_addr");
    require(Number.isInteger(offset) && offset > 0 && offset <= wasm.memory.buffer.byteLength - IO);
    io().set(password);
    const ke1 = readResult("login_start", 96, password.byteLength);
    require(!trapped);
    return Object.freeze({
      ke1,
      finish(bindings) {
        require(!consumed && closed === undefined && !trapped);
        consumed = true;
        try {
          const { ke2, context, clientIdentifier, verifierIdentifier } = bindings;
          require(ke2 instanceof Uint8Array && ke2.byteLength === 320 &&
            [context, clientIdentifier, verifierIdentifier].every(value =>
              value instanceof Uint8Array && value.byteLength === 32));
          const bytes = io();
          bytes.set(ke2, 0);
          bytes.set(context, 320);
          bytes.set(clientIdentifier, 352);
          bytes.set(verifierIdentifier, 384);
          const proof = readResult("login_finish", 64);
          require(!trapped);
          return proof;
        } catch {
          discard();
          throw new Error(REFUSAL);
        }
      },
      discard,
    });
  } catch {
    discard();
    throw new Error(REFUSAL);
  } finally {
    if (owned) Uint8Array.prototype.fill.call(password, 0);
    password = undefined;
  }
}
