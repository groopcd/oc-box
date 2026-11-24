// SPDX-License-Identifier: GPL-3.0-or-later
// Offline binding for a separately reviewed enrollment module. No loader.

const IO = 1024;
const MAX_SCRUB = 64 * IO;
const REFUSAL = "Named enrollment refused.";
const typedArray = Object.getPrototypeOf(Uint8Array.prototype);
const getter = key => Object.getOwnPropertyDescriptor(typedArray, key).get;
const byteType = getter(Symbol.toStringTag);
const bytesBuffer = getter("buffer");
const bytesOffset = getter("byteOffset");
const bytesLength = getter("byteLength");
const fill = Uint8Array.prototype.fill;
const ABI = Object.freeze({
  memory: "memory", io_addr: "function", io_size: "function", main: "function",
  registration_start: "function", registration_finish: "function",
  proof_finish: "function", discard: "function",
});

function require(ok) {
  if (!ok) throw new Error(REFUSAL);
}

function privateView(value) {
  require(byteType.call(value) === "Uint8Array");
  const buffer = bytesBuffer.call(value);
  require(buffer instanceof ArrayBuffer);
  return new Uint8Array(buffer, bytesOffset.call(value), bytesLength.call(value));
}

/** One synchronous, one-shot handle. Only start may obtain the local password. */
export function openEnrollmentAttempt(module) {
  let state = "empty";
  let wasm, offset, closed;
  let reading = false, running = false, uncertain = false;
  const invoke = operation => {
    running = true;
    try { return operation(); }
    catch {
      uncertain = true;
      throw new Error(REFUSAL);
    } finally { running = false; }
  };
  const io = () => {
    const buffer = wasm?.memory?.buffer;
    require(buffer && Number.isInteger(offset) && offset > 0 && offset <= buffer.byteLength - IO);
    return new Uint8Array(buffer, offset, IO);
  };
  const scrub = () => {
    try { fill.call(io(), 0); return true; } catch { return false; }
  };
  const discard = () => {
    if (closed !== undefined) return closed;
    if (reading || running) {
      // Do not re-enter an outstanding password transfer or Rust borrow.
      uncertain = true;
      return false;
    }
    state = "closed";
    let confirmed = !uncertain;
    if (wasm) {
      try {
        confirmed = !uncertain && invoke(() => wasm.discard()) === 1 && !uncertain;
      } catch {
        confirmed = false;
      } finally {
        confirmed = scrub() && confirmed;
        wasm = undefined;
      }
    }
    closed = confirmed;
    return closed;
  };
  const output = (operation, length) => {
    let result;
    try {
      require(invoke(operation) === length && !uncertain);
      result = io().slice(0, length); // Reacquire memory after Argon2 may grow it.
    } finally {
      if (!scrub()) uncertain = true;
    }
    if (uncertain) {
      if (result !== undefined) fill.call(result, 0);
      throw new Error(REFUSAL);
    }
    return result;
  };
  const stage = (bindings, fields) => {
    const dest = io();
    let offset = 0;
    for (const [name, size] of fields) {
      const bytes = privateView(Object.getOwnPropertyDescriptor(bindings, name)?.value);
      require(bytes.byteLength === size);
      dest.set(bytes, offset);
      offset += size;
    }
  };
  const preflight = () => {
    require(globalThis.isSecureContext === true && module instanceof WebAssembly.Module &&
      typeof globalThis.crypto?.getRandomValues === "function");
    const imports = WebAssembly.Module.imports(module);
    const exports = WebAssembly.Module.exports(module);
    require(imports.length === 1 && imports[0].module === "entropy" &&
      imports[0].name === "fill" && imports[0].kind === "function" &&
      exports.length === Object.keys(ABI).length &&
      exports.every(({ name, kind }) => Object.hasOwn(ABI, name) && ABI[name] === kind));
    const random = globalThis.crypto.getRandomValues.bind(globalThis.crypto);
    wasm = invoke(() => new WebAssembly.Instance(module, { entropy: {
      fill(ptr, length) {
        const buffer = wasm?.memory?.buffer;
        require(buffer && Number.isInteger(ptr) && ptr >= 0 &&
          Number.isInteger(length) && length >= 0 && length <= 65_536 &&
          ptr <= buffer.byteLength - length);
        random(new Uint8Array(buffer, ptr, length));
      },
    } }).exports);
    require(wasm.memory instanceof WebAssembly.Memory && invoke(() => wasm.io_size()) === IO);
    offset = invoke(() => wasm.io_addr());
    io(); // The trusted reader is not called before the ABI and buffer pass.
  };
  return Object.freeze({
    start(readPassword) {
      require(state === "empty");
      state = "opening";
      try {
        require(typeof readPassword === "function");
        preflight();
        let supplied;
        reading = true;
        try { supplied = readPassword(); }
        finally { reading = false; }
        let password;
        try {
          password = privateView(supplied);
          require(password.byteLength <= MAX_SCRUB);
        } catch {
          uncertain = true; // An unsupported view cannot be scrubbed safely.
          throw new Error(REFUSAL);
        }
        const size = password.byteLength;
        try {
          require(!uncertain && size >= 1 && size <= IO);
          io().set(password);
        } finally {
          try { fill.call(password, 0); }
          catch {
            uncertain = true;
            throw new Error(REFUSAL);
          }
        }
        const request = output(() => wasm.registration_start(size), 32);
        state = "registering";
        return request;
      } catch {
        discard();
        throw new Error(REFUSAL);
      }
    },
    finishRegistration(bindings) {
      require(state === "registering");
      state = "processing";
      try {
        stage(bindings, [["response", 64], ["clientIdentifier", 32], ["verifierIdentifier", 32]]);
        const outputBytes = output(() => wasm.registration_finish(), 192 + 96);
        state = "proving";
        return Object.freeze({
          registration: outputBytes.subarray(0, 192),
          ke1: outputBytes.subarray(192),
        });
      } catch {
        discard();
        throw new Error(REFUSAL);
      }
    },
    finishProof(bindings) {
      require(state === "proving");
      state = "spent";
      try {
        stage(bindings, [["ke2", 320], ["context", 32]]);
        return output(() => wasm.proof_finish(), 64);
      } catch {
        discard();
        throw new Error(REFUSAL);
      }
    },
    discard,
  });
}
