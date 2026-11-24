// SPDX-License-Identifier: GPL-3.0-or-later
// Synthetic account page only; password never enters a serialized form.
import { createLocalPakeClient } from "./transport.mjs";
import { openLoginAttempt } from "./slot.mjs";
import { createWorkspaceTransfer } from "./transfer.mjs";

const form = document.getElementById("login-form");
const user = document.getElementById("login-user");
const password = document.getElementById("login-password");
const button = document.getElementById("login-submit");
const next = document.getElementById("login-continue");
const status = document.getElementById("login-status");
const accountOrigin = document.querySelector('meta[name="ocbox-account-origin"]').content;
const workspaceOrigin = document.querySelector('meta[name="ocbox-workspace-origin"]').content;
const cancelled = new AbortController();
let auth;
let transfer, ticket;

const show = (state, text) => {
  form.dataset.state = state;
  status.textContent = text;
};
const enable = on => {
  user.disabled = password.disabled = button.disabled = !on;
};

addEventListener("pagehide", () => {
  password.value = "";
  cancelled.abort();
  ticket?.discard();
  ticket = undefined;
  next.disabled = true;
  enable(false);
}, { once: true });

next.addEventListener("click", event => {
  if (!event.isTrusted || !ticket || cancelled.signal.aborted) return;
  const carrier = ticket;
  ticket = undefined;
  next.disabled = true;
  next.hidden = true;
  try {
    transfer(carrier);
  } catch {
    show("refused", "Named sign-in refused.");
    enable(true);
  } finally { carrier.discard(); }
});

function readPassword() {
  // Fixed scratch size; never stringify the PAKE bytes or persist them.
  const scratch = new Uint8Array(1024);
  let owned = true;
  try {
    const text = password.value;
    if (text.length < 1 || text.length > 128) throw new Error("Named sign-in refused.");
    const { read, written } = new TextEncoder().encodeInto(text, scratch);
    if (read !== text.length || written < 1) throw new Error("Named sign-in refused.");
    const bytes = scratch.subarray(0, written);
    owned = false; // The slot owns and wipes this view. The unused tail is zero.
    return bytes;
  } finally {
    password.value = "";
    if (owned) scratch.fill(0);
  }
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  if (!auth || button.disabled || cancelled.signal.aborted) {
    password.value = "";
    return;
  }
  const username = user.value;
  enable(false);
  show("running", "Checking the local proof…");
  try {
    let pending;
    try {
      pending = auth.exchange({ username, readPassword, signal: cancelled.signal });
    } finally {
      // The exact adapter reader is synchronous. Also clear when validation
      // stops the exchange before it ever invokes readPassword.
      password.value = "";
    }
    ticket = await pending;
    if (cancelled.signal.aborted) {
      ticket.discard();
      ticket = undefined;
      return;
    }
    next.hidden = false;
    next.disabled = false;
    show("verified", "Proof verified. Continue to confirm this browser.");
  } catch {
    if (!cancelled.signal.aborted) show("refused", "Named sign-in refused.");
  } finally {
    password.value = "";
    if (!cancelled.signal.aborted && form.dataset.state !== "verified") enable(true);
  }
});

try {
  // These asset bytes are verified by the fixture before it binds a port.
  // The trusted account server configures both origins, never request URLs.
  const module = await WebAssembly.compileStreaming(fetch("/account-assets/client.wasm", {
    mode: "same-origin", credentials: "same-origin", redirect: "error", cache: "no-store",
  }));
  auth = createLocalPakeClient({
    accountOrigin, workspaceOrigin, openAttempt: () => openLoginAttempt(module),
  });
  transfer = createWorkspaceTransfer({ accountOrigin, workspaceOrigin });
  if (!cancelled.signal.aborted) {
    show("ready", "Use the synthetic web account for this test.");
    enable(true);
  }
} catch {
  show("unavailable", "Named sign-in is unavailable.");
}
