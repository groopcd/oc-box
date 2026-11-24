// SPDX-License-Identifier: GPL-3.0-or-later
// Trusted, isolated account document only. No raw-password or guest fallback.
import { createLocalPakeClient } from "./transport.mjs";
import { openLoginAttempt } from "./slot.mjs";
import { createWorkspaceTransfer } from "./transfer.mjs";

const page = document;
const element = id => page.getElementById(id);
const meta = name => page.querySelector(`meta[name="${name}"]`).content;
const form = element("login-form");
const user = element("login-user");
const password = element("login-password");
const button = element("login-submit");
const reveal = element("login-show");
const next = element("login-continue");
const status = element("login-error");
const accountOrigin = meta("ocbox-account-origin");
const workspaceOrigin = meta("ocbox-workspace-origin");
const cancelled = new AbortController();
const REFUSAL = "Named sign-in refused.";
const MAX_WASM = 1024 * 1024;
let auth, transfer, ticket;

const require = condition => { if (!condition) throw new Error(REFUSAL); };
const current = () => window === top && page === document && isSecureContext === true &&
  location.origin === accountOrigin && location.pathname === "/auth/oc-box-account-entry" &&
  !location.search && !location.hash && !navigator.serviceWorker?.controller &&
  !cancelled.signal.aborted;
const trusted = event => current() && event.isTrusted && navigator.userActivation?.isActive === true;
const show = (state, text) => {
  form.dataset.state = state;
  status.textContent = text;
};
const enable = on => { user.disabled = password.disabled = button.disabled = reveal.disabled = !on; };
const mask = masked => {
  password.type = masked ? "password" : "text";
  reveal.textContent = masked ? "Show" : "Hide";
  reveal.setAttribute("aria-pressed", String(!masked));
};
const clear = () => { password.value = ""; mask(true); };

addEventListener("pagehide", () => {
  clear();
  cancelled.abort();
  ticket?.discard();
  ticket = undefined;
  enable(false);
  next.disabled = true;
}, { once: true });

// Neither requestSubmit() nor dispatching a submit event can read a password.
// The unnamed controls also cannot serialize into a native fallback form.
form.addEventListener("submit", event => event.preventDefault());
reveal.addEventListener("click", event => {
  if (!trusted(event) || reveal.disabled) return;
  mask(password.type !== "password");
  password.focus();
});
next.addEventListener("click", event => {
  if (!trusted(event) || !ticket || next.disabled) return;
  const carrier = ticket;
  ticket = undefined;
  next.disabled = next.hidden = true;
  try {
    transfer(carrier);
  } catch {
    show("refused", REFUSAL);
    enable(current());
  } finally { carrier.discard(); }
});

function readPassword() {
  const scratch = new Uint8Array(1024);
  let owned = true;
  try {
    require(current());
    const text = password.value;
    require(text.length > 0 && text.length <= scratch.length);
    // TextEncoder replaces lone surrogates; refuse instead of changing input.
    for (let index = 0; index < text.length; index++) {
      const code = text.charCodeAt(index);
      if (code >= 0xd800 && code <= 0xdbff) {
        const low = text.charCodeAt(++index);
        require(low >= 0xdc00 && low <= 0xdfff);
      } else {
        require(code < 0xdc00 || code > 0xdfff);
      }
    }
    const { read, written } = new TextEncoder().encodeInto(text, scratch);
    require(read === text.length && written > 0);
    const bytes = scratch.subarray(0, written);
    owned = false; // The slot owns and wipes the view; the rest is still zero.
    return bytes;
  } finally {
    clear();
    if (owned) scratch.fill(0);
  }
}

async function login(event) {
  if (!trusted(event) || !auth || button.disabled) return;
  event.preventDefault();
  const username = user.value;
  mask(true);
  enable(false);
  password.removeAttribute("aria-invalid");
  show("running", "Checking the local proof…");
  try {
    let pending;
    try {
      pending = auth.exchange({ username, readPassword, signal: cancelled.signal });
    } finally {
      // The exact adapter reader is synchronous, or validation stops first.
      clear();
    }
    ticket = await pending;
    if (!current()) {
      ticket.discard();
      ticket = undefined;
      return;
    }
    next.hidden = next.disabled = false;
    show("verified", "Proof verified. Continue to confirm this browser.");
    next.focus();
  } catch {
    if (current()) {
      password.setAttribute("aria-invalid", "true");
      show("refused", REFUSAL);
    }
  } finally {
    clear();
    if (current() && form.dataset.state !== "verified") {
      enable(true);
      password.focus();
    }
  }
}

button.addEventListener("click", login);
for (const field of [user, password]) {
  field.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.repeat && !event.isComposing) void login(event);
  });
}

async function loadWasm() {
  const sha256 = meta("ocbox-wasm-sha256");
  const expected = meta("ocbox-wasm-size");
  const size = Number(expected);
  require(/^[a-f0-9]{64}$/.test(sha256) && sha256.length === 64 &&
    Number.isInteger(size) && String(size) === expected && size > 8 && size <= MAX_WASM);
  const url = accountOrigin + "/account-assets/client.wasm";
  const response = await fetch(url, {
    mode: "same-origin", credentials: "same-origin", redirect: "error", cache: "no-store",
    referrerPolicy: "origin", signal: cancelled.signal,
  });
  require(response.status === 200 && response.type === "basic" && response.url === url &&
    !response.redirected && response.headers.get("content-type") === "application/wasm" &&
    response.headers.get("content-length") === expected &&
    response.headers.get("cache-control") === "no-store" &&
    response.headers.get("content-encoding") === null);
  const reader = response.body.getReader();
  const wasm = new Uint8Array(size);
  let at = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      require(value instanceof Uint8Array && value.byteLength > 0 && value.byteLength <= size - at);
      wasm.set(value, at);
      at += value.byteLength;
    }
  } finally { reader.releaseLock(); }
  require(at === size);
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", wasm));
  require(Array.from(digest, byte => byte.toString(16).padStart(2, "0")).join("") === sha256);
  return WebAssembly.compile(wasm);
}

const startup = setTimeout(() => cancelled.abort(), 10000);
try {
  require(current() && [...form.elements].every(field => !field.hasAttribute("name")));
  const module = await loadWasm();
  require(current());
  auth = createLocalPakeClient({ accountOrigin, workspaceOrigin, openAttempt: () => openLoginAttempt(module) });
  transfer = createWorkspaceTransfer({ accountOrigin, workspaceOrigin });
  show("ready", "");
  enable(true);
} catch {
  cancelled.abort();
  clear();
  enable(false);
  show("unavailable", "Named sign-in is unavailable.");
} finally { clearTimeout(startup); }
