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
const REFUSAL = "Named sign-in refused.";

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

function checkPage(origin) {
  require(globalThis.window === globalThis && globalThis.top === globalThis &&
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

  constructor(value) {
    this.#value = value;
    Object.freeze(this);
  }

  // Only for a future, independently reviewed POST handoff. Never a URL.
  take() {
    const value = this.#value;
    this.#value = null;
    return value;
  }
}

/**
 * Origins come from trusted account-page configuration, never the URL, native
 * frame or an ingress reply. Call from the trusted top-level account page only.
 *
 * A trusted caller prepares a fresh, synchronous, bounded WASM session per
 * attempt via createSession(). The binding alone owns password preparation and
 * lifetime. It returns {ke1: Uint8Array, finish(bindings): Uint8Array, discard():
 * true}. finish MUST verify the server's OPAQUE proof before emitting KE3.
 * discard MUST confirm cleanup, even after finish or refusal. Neither callback
 * failures nor returned byte shapes attest to provenance or authentication.
 */
export function createLocalPakeClient(config) {
  let accountOrigin, workspaceOrigin;
  try {
    ({ accountOrigin, workspaceOrigin } = config);
    require(localPort(accountOrigin, ACCOUNT_HOST) !==
      localPort(workspaceOrigin, WORKSPACE_HOST));
    checkPage(accountOrigin);
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
      const controller = new AbortController();
      const cancel = () => controller.abort(); // Do not forward arbitrary abort reasons.
      const deadline = performance.now() + EXCHANGE_MS;
      let watchedSignal, timer, session, cleanupPending = false;
      const checkpoint = () => {
        require(!controller.signal.aborted && performance.now() < deadline);
        checkPage(accountOrigin);
      };
      const discard = () => {
        if (!cleanupPending) return true;
        cleanupPending = false;
        try {
          if (localResult(session?.discard()) === true) return true;
        } catch {
          // A failed cleanup fences this client. Never expose the binding error.
        } finally {
          session = undefined;
        }
        fenced = true;
        return false;
      };

      try {
        let { username, createSession, signal: suppliedSignal } = input;
        input = undefined;
        require(matches(/^[a-z0-9][a-z0-9_-]{0,31}$/, username) &&
          typeof createSession === "function" &&
          (suppliedSignal === undefined || suppliedSignal instanceof AbortSignal));
        if (suppliedSignal !== undefined) {
          AbortSignal.prototype.throwIfAborted.call(suppliedSignal);
          EventTarget.prototype.addEventListener.call(suppliedSignal, "abort", cancel, { once: true });
          watchedSignal = suppliedSignal;
        }
        globalThis.addEventListener("pagehide", cancel, { once: true });
        timer = setTimeout(cancel, EXCHANGE_MS);
        checkpoint();
        cleanupPending = true;
        session = localResult(createSession());
        createSession = undefined;
        require(typeof session?.finish === "function" && typeof session?.discard === "function");
        const start = await post(accountOrigin, "/start",
          { username, ke1: encode(localResult(session.ke1), SIZES.ke1) },
          ["challenge_id", "ke2", "context"], controller.signal, checkpoint);
        require(matches(/^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$/, start.challenge_id));
        const ke2 = decode(start.ke2, SIZES.ke2);
        const context = await binding(PROFILE, accountOrigin, workspaceOrigin, start.challenge_id);
        require(encode(context, 32) === start.context);
        // The separate "credential" identifier is used only by the verifier.
        const clientIdentifier = await binding(PROFILE, "client", username);
        const verifierIdentifier = await binding(PROFILE, "verifier", accountOrigin);
        checkpoint();
        const proof = localResult(session.finish({ ke2, context, clientIdentifier, verifierIdentifier }));
        const ke3 = encode(proof, SIZES.ke3);
        require(discard()); // Do not send proof with uncertain local cleanup.
        const finish = await post(accountOrigin, "/finish",
          { challenge_id: start.challenge_id, ke3 }, ["ticket"], controller.signal, checkpoint);
        require(matches(/^[A-Za-z0-9_-]{53}[AQgw]$/, finish.ticket));
        checkpoint();
        return new HandoffTicket(finish.ticket);
      } catch {
        throw new Error(REFUSAL);
      } finally {
        cancel();
        clearTimeout(timer);
        globalThis.removeEventListener("pagehide", cancel);
        if (watchedSignal) {
          EventTarget.prototype.removeEventListener.call(watchedSignal, "abort", cancel);
        }
        discard();
        busy = false;
      }
    },
  });
}
