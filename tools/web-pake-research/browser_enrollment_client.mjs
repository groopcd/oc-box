// SPDX-License-Identifier: GPL-3.0-or-later
// Checkout-only, one-shot account-page transport. No input form or asset loader.

const PROFILE = "oc-box.named.local/OPAQUE-25519-SHA512-v1";
const REFUSAL = "Named enrollment refused.";
const MAX_REPLY = 512;
const EXCHANGE_MS = 30_000;
const mountedPages = new WeakSet();
const readAborted = Object.getOwnPropertyDescriptor(AbortSignal.prototype, "aborted").get;
const fill = Uint8Array.prototype.fill;

function require(ok) {
  if (!ok) throw new Error(REFUSAL);
}

function matches(pattern, value) {
  return typeof value === "string" && pattern.exec(value)?.[0] === value;
}

function port(origin, hostname) {
  require(typeof origin === "string" && origin.length <= hostname.length + 14);
  const parsed = new URL(origin);
  const port = Number(parsed.port);
  require(parsed.protocol === "https:" && parsed.hostname === hostname &&
    parsed.origin === origin && port >= 1024 && port <= 65535);
  return port;
}

function checkPage(origin, page) {
  require(globalThis.window === globalThis && globalThis.top === globalThis &&
    page && globalThis.document === page && globalThis.isSecureContext === true &&
    globalThis.location?.origin === origin && !globalThis.navigator?.serviceWorker?.controller);
}

function encode(bytes, size) {
  require(bytes instanceof Uint8Array && bytes.byteLength === size);
  return btoa(String.fromCharCode(...bytes));
}

function encodedOutput(value, size) {
  require(value instanceof Uint8Array && value.byteLength === size &&
    value.buffer instanceof ArrayBuffer);
  try { return encode(value, size); }
  finally { fill.call(value, 0); }
}

function decode(text, size) {
  require(typeof text === "string" && text.length === Math.ceil(size / 3) * 4);
  const raw = atob(text);
  require(raw.length === size && btoa(raw) === text);
  return Uint8Array.from(raw, char => char.charCodeAt(0));
}

async function binding(label, value) {
  const encoder = new TextEncoder();
  const input = [PROFILE, label, value].flatMap(part => {
    const bytes = encoder.encode(part);
    return [bytes.length >> 8, bytes.length & 255, ...bytes];
  });
  return new Uint8Array(await crypto.subtle.digest("SHA-256", Uint8Array.from(input)));
}

async function post(origin, path, payload, length, fields, signal, checkpoint) {
  const body = JSON.stringify(payload);
  require(body.length === length);
  const url = origin + path;
  checkpoint();
  const response = await fetch(url, {
    method: "POST", mode: "same-origin", credentials: "same-origin",
    redirect: "error", cache: "no-store", referrerPolicy: "origin",
    keepalive: false, headers: { "Content-Type": "application/json" }, signal, body,
  });
  checkpoint();
  const count = response.headers.get("content-length");
  require(response.status === 200 && response.type === "basic" &&
    response.redirected === false && response.url === url &&
    response.headers.get("content-type") === "application/json" &&
    response.headers.get("cache-control") === "no-store" &&
    response.headers.get("content-encoding") === null &&
    matches(/^[1-9][0-9]{0,2}$/, count) && Number(count) <= MAX_REPLY);
  const bytes = new Uint8Array(Number(count));
  const reader = response.body.getReader();
  let offset = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      checkpoint();
      if (done) break;
      require(value instanceof Uint8Array && value.byteLength > 0 &&
        value.byteLength <= bytes.length - offset);
      bytes.set(value, offset);
      offset += value.byteLength;
    }
  } finally { reader.releaseLock(); }
  require(offset === bytes.length && bytes.every(byte => byte < 128));
  const text = String.fromCharCode(...bytes);
  const parsed = JSON.parse(text);
  require(parsed && typeof parsed === "object" && !Array.isArray(parsed) &&
    fields.every(key => Object.hasOwn(parsed, key)));
  const canonical = Object.fromEntries(fields.map(key => [key, parsed[key]]));
  require(JSON.stringify(canonical) === text);
  return canonical;
}

/**
 * All configuration comes from the trusted, explicitly host-approved page.
 * The input has only a one-shot local reader and optional abort signal.
 * Success is a proof verdict, never an app grant or a committed enrollment.
 */
export function createLocalEnrollmentClient(config) {
  let accountOrigin, workspaceOrigin, approvedUsername, openAttempt, page;
  try {
    ({ accountOrigin, workspaceOrigin, approvedUsername, openAttempt } = config);
    require(port(accountOrigin, "accounts.ocbox.test") !== port(workspaceOrigin, "workspace.ocbox-app.test") &&
      matches(/^[a-z0-9][a-z0-9_-]{0,31}$/, approvedUsername) &&
      typeof openAttempt === "function");
    page = globalThis.document;
    checkPage(accountOrigin, page);
    require(!mountedPages.has(page));
    mountedPages.add(page);
  } catch { throw new Error(REFUSAL); }

  let used = false;
  return Object.freeze({
    async enroll(input) {
      require(!used);
      used = true;
      let controller, watched, timer, attempt, dispose, watchingPage = false;
      let readPassword, cleanupPending = false;
      const deadline = performance.now() + EXCHANGE_MS;
      const abort = () => controller?.abort();
      const cancelled = () => watched !== undefined && readAborted.call(watched);
      const onAbort = () => { if (cancelled()) abort(); };
      const checkpoint = () => {
        require(!controller.signal.aborted && !cancelled() && performance.now() < deadline);
        checkPage(accountOrigin, page);
      };
      const local = value => {
        if (value instanceof Promise) {
          Promise.prototype.then.call(value, undefined, () => {});
          throw new Error(REFUSAL);
        }
        return value;
      };
      const discard = () => {
        if (!cleanupPending) return true;
        cleanupPending = false;
        try { return typeof dispose === "function" && local(Reflect.apply(dispose, attempt, [])) === true; }
        catch { return false; }
        finally { attempt = dispose = undefined; }
      };
      const send = (path, body, size, fields) =>
        post(accountOrigin, path, body, size, fields, controller.signal, checkpoint);

      try {
        readPassword = Object.getOwnPropertyDescriptor(input, "readPassword")?.value;
        const signal = Object.getOwnPropertyDescriptor(input, "signal")?.value;
        input = undefined;
        require(typeof readPassword === "function");
        controller = new AbortController();
        if (signal !== undefined) {
          require(!readAborted.call(signal));
          EventTarget.prototype.addEventListener.call(signal, "abort", onAbort, { once: true });
          watched = signal;
        }
        globalThis.addEventListener("pagehide", abort, { once: true });
        watchingPage = true;
        timer = setTimeout(abort, EXCHANGE_MS);
        checkpoint();
        cleanupPending = true;
        attempt = local(openAttempt());
        dispose = attempt?.discard;
        const start = attempt?.start, register = attempt?.finishRegistration, prove = attempt?.finishProof;
        require([dispose, start, register, prove].every(value => typeof value === "function"));
        let read = false;
        const take = () => {
          checkpoint();
          require(readPassword !== undefined);
          const reader = readPassword;
          readPassword = undefined;
          const result = local(reader());
          read = true;
          return result;
        };
        let request;
        try {
          request = encodedOutput(local(Reflect.apply(start, attempt, [take])), 32);
          require(read);
        } finally { readPassword = undefined; }

        const started = await send("/enroll/start", { request }, 58, ["challenge", "response"]);
        const challenge = started.challenge;
        require(matches(/^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$/, challenge));
        const response = decode(started.response, 64);
        const clientIdentifier = await binding("client", approvedUsername);
        const verifierIdentifier = await binding("verifier", accountOrigin);
        const context = await binding("enrollment-proof", `${accountOrigin}\n${challenge}`);
        checkpoint();
        const upload = local(Reflect.apply(register, attempt, [{
          response, clientIdentifier, verifierIdentifier,
        }]));
        const registration = encodedOutput(upload.registration, 192);
        const ke1 = encodedOutput(upload.ke1, 96);
        const pending = await send("/enroll/proof/start",
          { challenge, registration, ke1 }, 470, ["ke2", "context"]);
        require(encode(context, 32) === pending.context);
        const ke2 = decode(pending.ke2, 320);
        checkpoint();
        const ke3 = encodedOutput(local(Reflect.apply(prove, attempt, [{ ke2, context }])), 64);
        // No proof request is sent if local private-state teardown is uncertain.
        require(discard());
        const finished = await send("/enroll/proof/finish", { challenge, ke3 }, 156, ["verified"]);
        checkpoint();
        require(finished.verified === true);
        return true;
      } catch {
        throw new Error(REFUSAL);
      } finally {
        abort();
        clearTimeout(timer);
        if (watchingPage) globalThis.removeEventListener("pagehide", abort);
        if (watched !== undefined) {
          EventTarget.prototype.removeEventListener.call(watched, "abort", onAbort);
        }
        readPassword = undefined;
        discard();
      }
    },
  });
}
