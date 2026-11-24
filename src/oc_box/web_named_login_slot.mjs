// SPDX-License-Identifier: GPL-3.0-or-later
// Offline glue: the trusted caller supplies the verified module and bytes.

const IO = 1024;
const MAX_SCRUB = 64 * IO;
const MAX_ENTROPY = 65_536; // WebCrypto limit per call.
const REFUSAL = "Named sign-in refused.";
const typedArray = Object.getPrototypeOf(Uint8Array.prototype);
const getter = key => Object.getOwnPropertyDescriptor(typedArray, key).get;
const byteType = getter(Symbol.toStringTag);
const bytesBuffer = getter("buffer");
const bytesOffset = getter("byteOffset");
const bytesLength = getter("byteLength");

function require(ok) {
  if (!ok) throw new Error(REFUSAL);
}

function privateView(value) {
  require(byteType.call(value) === "Uint8Array");
  const buffer = bytesBuffer.call(value);
  require(buffer instanceof ArrayBuffer);
  // Rebuild only the view, not the bytes. Do not call methods/getters on a
  // provided view or accept a SharedArrayBuffer that can change concurrently.
  return new Uint8Array(buffer, bytesOffset.call(value), bytesLength.call(value));
}

/**
 * Synchronous, empty attempt for openAttempt() in web_pake_client.mjs.
 * The verified module is fixed for this handle. start(reader) invokes reader
 * once, only after preflighting the WASM ABI. The reader transfers ownership
 * of a private mutable 1–1024-byte UTF-8 view. No password is passed to finish.
 */
export function openLoginAttempt(module) {
  let state = "empty";
  let wasm, offset, closed;
  let executing = false, reading = false, trapped = false, uncertainInput = false;
  const io = () => {
    const buffer = wasm?.memory?.buffer;
    require(buffer && Number.isInteger(offset) && offset > 0 && offset <= buffer.byteLength - IO);
    return new Uint8Array(buffer, offset, IO);
  };
  const scrub = () => {
    try { io().fill(0); return true; } catch { return false; }
  };
  const invoke = operation => {
    executing = true;
    try {
      return operation();
    } catch {
      trapped = true;
      throw new Error(REFUSAL);
    } finally {
      executing = false;
    }
  };
  const discard = () => {
    if (closed !== undefined) return closed;
    if (executing) {
      // No re-entry into Rust while it may hold the slot's mutable borrow.
      // The outer operation will drop it as an unconfirmed cleanup.
      trapped = true;
      return false;
    }
    if (reading) {
      // The reader has not returned its bytes; a reentrant caller cannot
      // claim these are gone. The outer start will scrub what it receives.
      uncertainInput = true;
      return false;
    }
    state = "closed";
    let confirmed = !trapped && !wasm;
    if (wasm) {
      try {
        // A trap may have stranded Rust's borrow and private copies. Scrub
        // only accessible linear memory; never re-enter or claim full cleanup.
        confirmed = !trapped && invoke(() => wasm.discard()) === 1 && !trapped;
      } catch {
        confirmed = false;
      } finally {
        confirmed = scrub() && confirmed;
        wasm = undefined;
      }
    }
    closed = confirmed && !uncertainInput;
    return closed;
  };
  const readResult = (operation, expected) => {
    try {
      require(invoke(operation) === expected && !trapped);
      // Argon2 may grow memory, detaching previous views.
      return io().slice(0, expected);
    } finally {
      if (!scrub()) trapped = true;
    }
  };
  return Object.freeze({
    start(readPassword) {
      require(state === "empty");
      state = "starting";
      try {
        require(typeof readPassword === "function" && globalThis.isSecureContext === true &&
          typeof globalThis.crypto?.getRandomValues === "function" &&
          module instanceof WebAssembly.Module);
        const imports = WebAssembly.Module.imports(module);
        require(imports.length === 1 && imports[0].module === "entropy" &&
          imports[0].name === "fill" && imports[0].kind === "function");
        const random = globalThis.crypto.getRandomValues.bind(globalThis.crypto);
        wasm = invoke(() => new WebAssembly.Instance(module, { entropy: {
          fill(ptr, length) {
            const buffer = wasm?.memory?.buffer;
            require(buffer && Number.isInteger(ptr) && ptr >= 0 &&
              Number.isInteger(length) && length >= 0 && length <= MAX_ENTROPY &&
              ptr <= buffer.byteLength - length);
            random(new Uint8Array(buffer, ptr, length));
          },
        } }).exports);
        require(wasm.memory instanceof WebAssembly.Memory &&
          ["io_size", "io_addr", "login_start", "login_finish", "discard"]
            .every(name => typeof wasm[name] === "function") &&
          invoke(() => wasm.io_size()) === IO);
        offset = invoke(() => wasm.io_addr());
        io(); // Validate the shared I/O slice before handing over the reader.
        let supplied;
        reading = true;
        try { supplied = readPassword(); }
        finally { reading = false; }
        let password;
        try {
          password = privateView(supplied);
          require(password.byteLength <= MAX_SCRUB);
        } catch {
          // A reader violating the private-view contract may leave bytes we
          // cannot safely clear in bounded time. Do not confirm teardown.
          uncertainInput = true;
          throw new Error(REFUSAL);
        }
        const size = password.byteLength;
        try {
          require(!uncertainInput && size >= 1 && size <= IO);
          io().set(password);
        } finally {
          try { password.fill(0); } catch {
            uncertainInput = true;
            throw new Error(REFUSAL);
          }
        }
        const ke1 = readResult(() => wasm.login_start(size), 96);
        require(!trapped);
        state = "pending";
        return ke1;
      } catch {
        discard();
        throw new Error(REFUSAL);
      }
    },
    finish(bindings) {
      require(state === "pending");
      state = "spent";
      try {
        // The adapter derives these immutable transcript values. Accept only
        // own data fields and snapshot into private WASM I/O without mutating
        // or retaining any caller view before invoking WASM.
        const frame = io();
        let at = 0;
        for (const [field, size] of [
          ["ke2", 320], ["context", 32], ["clientIdentifier", 32], ["verifierIdentifier", 32],
        ]) {
          const bytes = privateView(Object.getOwnPropertyDescriptor(bindings, field)?.value);
          require(bytes.byteLength === size);
          frame.set(bytes, at);
          at += size;
        }
        const proof = readResult(() => wasm.login_finish(), 64);
        require(!trapped);
        return proof;
      } catch {
        discard();
        throw new Error(REFUSAL);
      }
    },
    discard,
  });
}
