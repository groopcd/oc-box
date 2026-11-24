// Source-only transport for the local PAKE profile. No entry point, loader,
// bundled WASM, password input, handoff POST or workspace admission.

// Keep in step with web_local_origins, web_pake_profile, web_pake_worker,
// web_pake_flow, web_handoff and web_account_ingress.
const PROFILE = "oc-box.named.local/OPAQUE-25519-SHA512-v1";
const ACCOUNT_HOST = "accounts.ocbox.test";
const WORKSPACE_HOST = "workspace.ocbox-app.test";
const SIZES = Object.freeze({ ke1: 96, ke2: 320, ke3: 64 });
const MAX_BODY_BYTES = 8192;
const EXCHANGE_MS = 30_000; // Local deadline, not a wire field or the server TTL.
const TICKET_MS = 5_000; // Local retention budget; the server can expire it sooner.
const REFUSAL = "Named sign-in refused.";
const mountedPages = new WeakSet();
const readAbortState = Object.getOwnPropertyDescriptor(AbortSignal.prototype, "aborted").get;

function require(condition) {
  if (!condition) throw new Error(REFUSAL);
}

function matches(pattern, value) {
  return typeof value === "string" && pattern.exec(value)?.[0] === value;
}

function localPort(origin, hostname) {
  require(typeof origin === "string" && origin.length <= hostname.length + 14);
  const parsed = new URL(origin);
  const port = Number(parsed.port);
  require(parsed.protocol === "https:" && parsed.hostname === hostname &&
    parsed.origin === origin && port >= 1024 && port <= 65535);
  return port;
}

function checkPage(origin, page) {
  require(globalThis.window === globalThis && globalThis.top === globalThis &&
    page && globalThis.document === page &&
    globalThis.isSecureContext === true && globalThis.location?.origin === origin &&
    !globalThis.navigator?.serviceWorker?.controller);
}

function encode(bytes, size) {
  require(bytes instanceof Uint8Array && bytes.byteLength === size);
  return btoa(String.fromCharCode(...bytes));
}

function decode(text, size) {
  require(text.length === Math.ceil(size / 3) * 4);
  const raw = atob(text);
  require(raw.length === size && btoa(raw) === text);
  return Uint8Array.from(raw, char => char.charCodeAt(0));
}

// Public transcript metadata only: Python uses an ASCII UTF-8 subset and
// prefixes each part with its uint16 big-endian byte length. No password hash,
// key derivation or PAKE operations belong in this module.
async function binding(...parts) {
  const encoder = new TextEncoder();
  const bytes = parts.flatMap(part => {
    const raw = encoder.encode(part);
    return [raw.length >> 8, raw.length & 255, ...raw];
  });
  return new Uint8Array(await crypto.subtle.digest("SHA-256", Uint8Array.from(bytes)));
}

async function readReply(response, url, fields, checkpoint) {
  checkpoint();
  const length = response.headers.get("content-length");
  require(response.status === 200 && response.type === "basic" &&
    response.redirected === false && response.url === url &&
    response.headers.get("content-type") === "application/json" &&
    response.headers.get("cache-control") === "no-store" &&
    response.headers.get("content-encoding") === null &&
    matches(/^[1-9][0-9]{0,3}$/, length) && Number(length) <= MAX_BODY_BYTES);
  const bytes = new Uint8Array(Number(length));
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
  } finally {
    reader.releaseLock();
  }
  require(offset === bytes.length && bytes.every(byte => byte < 128));
  const text = String.fromCharCode(...bytes);
  const parsed = JSON.parse(text);
  const reply = Object.fromEntries(fields.map(key => [key, parsed[key]]));
  // AccountIngress._reply emits compact ASCII JSON in this field order.
  // Require that encoding rather than letting JSON.parse hide duplicate keys.
  require(fields.every(key => typeof reply[key] === "string") &&
    JSON.stringify(reply) === text);
  return reply;
}

async function post(origin, path, payload, fields, signal, checkpoint) {
  const body = JSON.stringify(payload);
  require(body.length <= MAX_BODY_BYTES);
  const url = origin + path;
  checkpoint();
  const response = await fetch(url, {
    method: "POST",
    mode: "same-origin",
    credentials: "same-origin",
    redirect: "error",
    cache: "no-store",
    // Send at most the origin as Referer. "no-referrer" on a non-CORS POST
    // can make Fetch serialize Origin as "null", which the ingress refuses.
    referrerPolicy: "origin",
    keepalive: false,
    headers: { "Content-Type": "application/json" },
    body,
    signal,
  });
  return readReply(response, url, fields, checkpoint);
}

class HandoffTicket {
  #value;
  #checkpoint;
  #release;

  constructor(value, checkpoint, release) {
    this.#value = value;
    this.#checkpoint = checkpoint;
    this.#release = release;
    Object.freeze(this);
  }

  // Only for a future, independently reviewed POST handoff. Never a URL.
  take() {
    if (this.#value === null) return null;
    try {
      this.#checkpoint();
      return this.#value;
    } catch {
      return null;
    } finally {
      this.discard();
    }
  }

  discard() {
    if (this.#value === null) return;
    this.#value = null;
    const release = this.#release;
    this.#checkpoint = this.#release = null;
    release();
  }
}

/**
 * Origins come from trusted account-page configuration, never the URL, native
 * frame or an ingress reply. Call from the trusted top-level account page only.
 *
 * The independently reviewed binding's openAttempt() must not handle any
 * password. It returns a fresh, discardable handle with synchronous, bounded
 * start(readPassword): Uint8Array, finish(bindings): Uint8Array and discard():
 * true. Only start may call the one-shot readPassword() for local bytes, which
 * the binding owns and must clean up. finish must verify the server proof
 * before emitting KE3. discard must confirm cleanup even after a local failure.
 * Cancellation gates progress; it cannot interrupt a synchronous WASM call.
 */
export function createLocalPakeClient(config) {
  let accountOrigin, workspaceOrigin, openAttempt, page;
  try {
    ({ accountOrigin, workspaceOrigin, openAttempt } = config);
    require(localPort(accountOrigin, ACCOUNT_HOST) !==
      localPort(workspaceOrigin, WORKSPACE_HOST));
    page = globalThis.document;
    checkPage(accountOrigin, page);
    require(typeof openAttempt === "function" && !mountedPages.has(page));
    mountedPages.add(page);
  } catch {
    throw new Error(REFUSAL);
  }

  let busy = false;
  let fenced = false;
  const localResult = value => {
    if (value instanceof Promise) {
      // An async binding is unsupported. Suppress its rejection, not the fence.
      fenced = true;
      Promise.prototype.then.call(value, undefined, () => {});
      throw new Error(REFUSAL);
    }
    return value;
  };
  return Object.freeze({
    async exchange(input) {
      require(!busy && !fenced);
      busy = true;
      let controller, watchedSignal, timer, deadline, ticket;
      let attempt, dispose, cleanupPending = false;
      let watchingPage = false;
      const cancel = () => {
        controller?.abort(); // Do not forward arbitrary abort reasons.
        ticket?.discard();
      };
      const signalCancelled = () => watchedSignal !== undefined && readAbortState.call(watchedSignal);
      const onSignalAbort = () => {
        if (signalCancelled()) cancel(); // Synthetic events do not change the signal's state.
      };
      const checkpoint = () => {
        require(!controller.signal.aborted && !signalCancelled() && performance.now() < deadline);
        checkPage(accountOrigin, page);
      };
      const release = () => {
        clearTimeout(timer);
        if (watchingPage) globalThis.removeEventListener("pagehide", cancel);
        if (watchedSignal !== undefined) {
          EventTarget.prototype.removeEventListener.call(watchedSignal, "abort", onSignalAbort);
        }
        controller?.abort();
        ticket = watchedSignal = undefined;
        busy = false;
      };
      const discard = () => {
        if (!cleanupPending) return true;
        cleanupPending = false;
        try {
          if (dispose && localResult(Reflect.apply(dispose, attempt, [])) === true) return true;
        } catch {
          // Unconfirmed attempt teardown permanently fences this page's client.
        } finally {
          attempt = dispose = undefined;
        }
        fenced = true;
        return false;
      };

      try {
        const { username, signal: suppliedSignal } = input;
        // Do not invoke a credential-reading accessor while validating the input.
        let readPassword = Object.getOwnPropertyDescriptor(input, "readPassword")?.value;
        input = undefined;
        require(matches(/^[a-z0-9][a-z0-9_-]{0,31}$/, username) &&
          typeof readPassword === "function");
        controller = new AbortController();
        deadline = performance.now() + EXCHANGE_MS;
        if (suppliedSignal !== undefined) {
          require(!readAbortState.call(suppliedSignal)); // Also checks the signal's brand.
          EventTarget.prototype.addEventListener.call(suppliedSignal, "abort", onSignalAbort);
          watchedSignal = suppliedSignal;
        }
        globalThis.addEventListener("pagehide", cancel, { once: true });
        watchingPage = true;
        timer = setTimeout(cancel, EXCHANGE_MS);
        checkpoint();
        cleanupPending = true;
        attempt = localResult(openAttempt());
        dispose = attempt?.discard;
        require(typeof dispose === "function");
        const begin = attempt.start;
        const complete = attempt.finish;
        require(typeof begin === "function" && typeof complete === "function");
        checkpoint();
        let passwordTaken = false;
        const takePassword = () => {
          checkpoint();
          require(readPassword !== undefined);
          const read = readPassword;
          readPassword = undefined;
          const bytes = localResult(read());
          require(bytes instanceof Uint8Array);
          passwordTaken = true;
          return bytes;
        };
        let ke1;
        try {
          ke1 = encode(localResult(Reflect.apply(begin, attempt, [takePassword])), SIZES.ke1);
          require(passwordTaken);
        } finally {
          readPassword = undefined;
        }
        const start = await post(accountOrigin, "/start",
          { username, ke1 },
          ["challenge_id", "ke2", "context"], controller.signal, checkpoint);
        require(matches(/^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$/, start.challenge_id));
        const ke2 = decode(start.ke2, SIZES.ke2);
        const context = await binding(PROFILE, accountOrigin, workspaceOrigin, start.challenge_id);
        require(encode(context, 32) === start.context);
        // The separate "credential" identifier is used only by the verifier.
        const clientIdentifier = await binding(PROFILE, "client", username);
        const verifierIdentifier = await binding(PROFILE, "verifier", accountOrigin);
        checkpoint();
        const proof = localResult(Reflect.apply(complete, attempt,
          [{ ke2, context, clientIdentifier, verifierIdentifier }]));
        const ke3 = encode(proof, SIZES.ke3);
        require(discard()); // Do not send proof with uncertain local cleanup.
        const finish = await post(accountOrigin, "/finish",
          { challenge_id: start.challenge_id, ke3 }, ["ticket"], controller.signal, checkpoint);
        require(matches(/^[A-Za-z0-9_-]{53}[AQgw]$/, finish.ticket));
        checkpoint();
        clearTimeout(timer);
        deadline = Math.min(deadline, performance.now() + TICKET_MS);
        timer = setTimeout(cancel, Math.max(0, deadline - performance.now()));
        ticket = new HandoffTicket(finish.ticket, checkpoint, release);
        return ticket;
      } catch {
        throw new Error(REFUSAL);
      } finally {
        discard();
        if (!ticket) release();
      }
    },
  });
}
