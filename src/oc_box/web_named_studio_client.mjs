// SPDX-License-Identifier: GPL-3.0-or-later
// Internal, first-party browser controller; this module mounts no routes.
import { createStudioShellView } from "./web_studio_shell.mjs";

const selectionPath = "/oc-box/studio/selection";
const conversationPath = "/oc-box/studio/conversation?session=";
const sendPath = "/oc-box/studio/send";
const pageHeader = "X-OC-Box-Studio-Page";
const appHeader = "X-OC-Box-Studio-App";
const generationHeader = "X-OC-Box-Studio-Generation";
const workspaceHost = "workspace.ocbox-app.test";
export const guestPagePath = "/auth/oc-box-studio-guest/";
const nonce = /^[A-Za-z0-9_-]{43}$/u;
export const guestNonce = /^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$/u;
const sessionIDPattern = /^ses[A-Za-z0-9_-]{1,125}$/u;
const json = "application/json";
const chatJSON = json + "; charset=utf-8";
const maxChange = 512;
const maxProjection = 512 * 1024;
const maxSendBody = 8 * 1024;
const maxText = 4 * 1024;
const exchangeMS = 7000;
const refreshOffsetsMS = Object.freeze([250, 1000, 2500]);
const refreshStartLimitMS = 3000;
const controls = /[\x00-\x08\x0b-\x1f\x7f-\x9f\p{Cs}]/u;
const encoder = new TextEncoder();
const decoder = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });
const mounted = new WeakSet();
const outcome = Object.freeze({ 403: "refused", 200: "queued", 503: "unconfirmed" });

function exactFields(value, keys) {
  if (!value || typeof value !== "object" || Reflect.ownKeys(value).length !== keys.length) {
    throw new TypeError();
  }
  const proto = Object.getPrototypeOf(value);
  if (proto !== Object.prototype && proto !== null) throw new TypeError();
  return keys.map(key => {
    const field = Object.getOwnPropertyDescriptor(value, key);
    if (!field?.enumerable || !Object.hasOwn(field, "value")) throw new TypeError();
    return field.value;
  });
}

function sessionID(value) {
  return typeof value === "string" && sessionIDPattern.test(value);
}

export async function readStudioJSON(response, url, type, limit, current) {
  const length = response.headers.get("content-length");
  if (!current() || response.type !== "basic" || response.redirected || response.url !== url ||
      response.headers.get("content-type") !== type || response.headers.has("content-encoding") ||
      response.headers.has("location") || response.headers.has("refresh") ||
      !length || !/^[1-9][0-9]{0,5}$/u.test(length) || Number(length) > limit || !response.body) {
    throw new TypeError();
  }
  const bytes = new Uint8Array(Number(length));
  const reader = response.body.getReader();
  let offset = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (!current()) throw new TypeError();
      if (done) break;
      if (!value.byteLength || value.byteLength > bytes.length - offset) throw new TypeError();
      bytes.set(value, offset);
      offset += value.byteLength;
    }
  } finally {
    reader.releaseLock();
  }
  if (offset !== bytes.length) throw new TypeError();
  const text = decoder.decode(bytes);
  const value = JSON.parse(text);
  // The host emits compact JSON in a fixed data profile. A lossy parse of
  // duplicate fields (including nested/escaped keys) cannot round-trip.
  if (JSON.stringify(value) !== text || !current()) throw new TypeError();
  return value;
}

/**
 * Trusted named-shell bootstrap only, with the exact injected page state.
 * The shell calls choose/home directly in a trusted click listener, passing
 * the exact checked session ID (not a DOM value). present() is a separate
 * readiness step after the existing shell's checked route matches.
 */
export function createNamedStudioController(root, bootstrap) {
  return createStudioController(root, bootstrap, false);
}

export function guestDocument(root) {
  const document = root?.ownerDocument;
  const window = document?.defaultView;
  if (!window || window.top !== window || window.isSecureContext !== true ||
      !root.isConnected || root.hasAttribute("data-named-studio") ||
      !root.hasAttribute("data-guest-studio") || window.navigator.serviceWorker?.controller ||
      window.document !== document || document.getElementById("web-shell") !== root ||
      document.hidden) return false;
  const { origin, port, pathname, search, hash } = window.location;
  return origin === `http://127.0.0.1:${port}` && /^[1-9][0-9]{0,4}$/u.test(port) &&
    Number(port) <= 65535 && pathname === guestPagePath && !search && !hash;
}

export function createGuestStudioController(root, bootstrap) {
  return guestDocument(root) ? createStudioController(root, bootstrap, true) : null;
}

function createStudioController(root, bootstrap, guest) {
  const document = root?.ownerDocument;
  const window = document?.defaultView;
  if (!window || mounted.has(document)) return null;
  const { origin, pathname } = window.location;
  const chrome = () => guest || root.hasAttribute("data-named-studio") ?
    window.ocBoxNamedStudioChrome : window.ocBoxWebShell;
  const shell = chrome();
  const port = Number(window.location.port);
  if (!guest && (window.location.protocol !== "https:" || window.location.hostname !== workspaceHost ||
      port < 1024 || port > 65535)) return null;
  let page, app;
  try {
    if (guest) {
      [app, page] = exactFields(bootstrap, ["app", "page"]);
      if (typeof app !== "string" || typeof page !== "string" ||
          !guestNonce.test(app) || !guestNonce.test(page)) return null;
    } else {
      const [handle, generation, id] = exactFields(bootstrap, ["page", "generation", "sessionID"]);
      if (typeof handle !== "string" || !nonce.test(handle) ||
          !Object.is(generation, 0) || id !== null) return null;
      page = handle;
    }
  } catch {
    return null;
  }

  let closed = false;
  let sequence = 0;
  let highestGeneration = 0;
  let change = null;
  let approved = null;
  const requests = new Set();
  const refreshTimers = new Set();
  const gestures = new WeakSet();

  function liveSite() {
    return !closed && window.top === window && window.isSecureContext === true &&
      window.document === document && root.ownerDocument === document && root.isConnected &&
      document.getElementById("web-shell") === root && !document.hidden &&
      chrome() === shell && (!guest || guestDocument(root)) && window.location.origin === origin &&
      window.location.pathname === pathname && window.location.hash === "" &&
      !window.navigator.serviceWorker?.controller;
  }

  function liveSelection() {
    const expected = approved;
    if (!expected || !liveSite()) return null;
    try {
      return shell.selection()?.sessionID === expected.sessionID && approved === expected ?
        expected : null;
    } catch {
      return null;
    }
  }

  const pageHeaders = () => ({ [pageHeader]: page, ...(guest ? { [appHeader]: app } : {}) });
  const scopeHeaders = scope => ({
    ...pageHeaders(),
    [generationHeader]: String(scope.generation),
  });

  async function exchange(path, body, headers, replyType, limit, statuses, current) {
    const abort = new window.AbortController();
    const deadline = window.performance.now() + exchangeMS;
    const timer = window.setTimeout(() => abort.abort(), exchangeMS);
    requests.add(abort);
    const live = () => !abort.signal.aborted && window.performance.now() < deadline && current();
    try {
      if (!live()) throw new TypeError();
      const url = origin + path;
      const response = await window.fetch(url, {
        method: body === undefined ? "GET" : "POST", body,
        headers: body === undefined ? headers : { ...headers, "Content-Type": json },
        credentials: "same-origin", mode: "same-origin", redirect: "error",
        cache: "no-store", referrerPolicy: "no-referrer", signal: abort.signal,
      });
      if (!Object.hasOwn(statuses, response.status)) throw new TypeError();
      const value = await readStudioJSON(response, url, replyType, limit, live);
      if (!live()) throw new TypeError();
      return { status: response.status, value };
    } finally {
      window.clearTimeout(timer);
      requests.delete(abort);
      abort.abort(); // Do not leave an unread body alive on refusal.
    }
  }

  function pinned(scope) {
    const exact = liveSelection();
    return exact && exact.sessionID === scope.sessionID && exact.generation === scope.generation ?
      exact : null;
  }

  const ok = Object.freeze({ 200: true });
  if (!liveSite()) return null;
  const view = createStudioShellView(root, Object.freeze({
    readSelection: liveSelection,
    async readTranscript(scope) {
      const exact = pinned(scope);
      if (!exact) return null;
      try {
        const { value } = await exchange(conversationPath + exact.sessionID, undefined,
          scopeHeaders(exact), chatJSON, maxProjection, ok, () => liveSelection() === exact);
        return liveSelection() === exact && value?.sessionID === exact.sessionID ? value : null;
      } catch {
        return null;
      }
    },
    // Not exposed to the caller: only the real Studio composer invokes this.
    async send(action) {
      const exact = pinned(action);
      const { text } = action;
      if (!exact || typeof text !== "string" || text.length > maxText || !text.trim() ||
          controls.test(text) || encoder.encode(text).byteLength > maxText) return "refused";
      const body = JSON.stringify({ sessionID: exact.sessionID, text });
      if (encoder.encode(body).byteLength > maxSendBody) return "refused";
      try {
        const { status, value } = await exchange(sendPath, body, scopeHeaders(exact),
          chatJSON, maxChange, outcome, () => liveSelection() === exact);
        const [result] = exactFields(value, ["status"]);
        if (result === outcome[status] && liveSelection() === exact) {
          if (result === "queued") {
            try { refreshAfterQueue(exact); }
            catch { stopRefreshes(); } // A view refresh cannot change the verified receipt.
          }
          return result;
        }
      } catch {
        // A missing, malformed, expired or interrupted reply is not refusal.
      }
      return "unconfirmed";
    },
  }));
  if (!view) return null;
  mounted.add(document);

  function stopRefreshes() {
    for (const timer of refreshTimers) window.clearTimeout(timer);
    refreshTimers.clear();
  }

  function refreshAfterQueue(scope) {
    stopRefreshes(); // A newer verified acknowledgement replaces pending samples.
    const latestStart = window.performance.now() + refreshStartLimitMS;
    for (const offset of refreshOffsetsMS) {
      const timer = window.setTimeout(() => {
        if (!refreshTimers.delete(timer)) return;
        if (liveSelection() !== scope) {
          if (approved === scope) invalidate();
          return;
        }
        // A delayed/background timer cannot extend this burst. The view also
        // skips a sample when an earlier read is still in flight.
        if (window.performance.now() >= latestStart) {
          stopRefreshes();
          return;
        }
        void view.refresh(scope).catch(() => { /* Keep the last valid projection. */ });
      }, offset);
      refreshTimers.add(timer);
    }
  }

  function invalidate() {
    change = approved = null;
    stopRefreshes();
    try {
      view.invalidate();
    } finally {
      for (const request of requests) request.abort();
      requests.clear();
    }
  }

  function consumeGesture(event) {
    const target = event?.currentTarget;
    if (!(event instanceof window.MouseEvent) || event.type !== "click" ||
        event.isTrusted !== true || event.button !== 0 || !event.eventPhase ||
        !(target instanceof window.Element) || !root.contains(target) || gestures.has(event)) {
      return false;
    }
    gestures.add(event);
    return true;
  }

  async function select(event, id) {
    if (!liveSite() || (id !== null && !sessionID(id)) || !consumeGesture(event)) return false;
    invalidate();
    if (sequence >= Number.MAX_SAFE_INTEGER) return false;
    const intent = { sequence: ++sequence };
    change = intent;
    const current = () => change === intent && liveSite();
    try {
      const changeRequest = { sequence: intent.sequence, sessionID: id };
      const data = JSON.stringify(guest ? changeRequest : { page, ...changeRequest });
      const { value } = await exchange(selectionPath, data, guest ? pageHeaders() : {},
        json, maxChange, ok, current);
      const [handle, generation, selected] = guest ?
        [page, ...exactFields(value, ["generation", "sessionID"])] :
        exactFields(value, ["page", "generation", "sessionID"]);
      if (!current() || handle !== page || selected !== id ||
          !Number.isSafeInteger(generation) || generation <= highestGeneration ||
          generation > intent.sequence) return false;
      highestGeneration = generation;
      if (id !== null) approved = Object.freeze({ sessionID: id, generation, writable: true });
      return true;
    } catch {
      return false;
    } finally {
      if (change === intent) change = null;
    }
  }

  const listeners = new window.AbortController();
  const options = { signal: listeners.signal };
  window.addEventListener("pagehide", invalidate, options);
  window.addEventListener("popstate", invalidate, options);
  window.addEventListener("hashchange", invalidate, options);
  document.addEventListener("visibilitychange", () => { if (document.hidden) invalidate(); }, options);
  return Object.freeze({
    choose: (event, id) => sessionID(id) ? select(event, id) : Promise.resolve(false),
    home: event => select(event, null),
    present: () => liveSelection() ? view.present() : Promise.resolve(false),
    invalidate,
    dispose() {
      closed = true;
      app = page = undefined;
      invalidate();
      listeners.abort();
    },
  });
}
