// SPDX-License-Identifier: GPL-3.0-or-later
// Synthetic account page only; no fallback, form serialization, or handoff.
import { createLocalPakeClient } from "./transport.mjs";
import { openLoginAttempt } from "./slot.mjs";

const form = document.getElementById("login-form");
const user = document.getElementById("login-user");
const password = document.getElementById("login-password");
const button = document.getElementById("login-submit");
const status = document.getElementById("login-status");
const accountOrigin = document.querySelector('meta[name="ocbox-account-origin"]').content;
const workspaceOrigin = document.querySelector('meta[name="ocbox-workspace-origin"]').content;
const cancelled = new AbortController();
let auth;

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
  enable(false);
}, { once: true });

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
  let ticket;
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
    if (cancelled.signal.aborted) return;
    // AccountIngress issued a real browser-bound ticket. It remains opaque;
    // this fixture neither takes it nor posts it to a workspace.
    show("complete", "Proof verified. No workspace was opened.");
  } catch {
    if (!cancelled.signal.aborted) show("refused", "Named sign-in refused.");
  } finally {
    ticket?.discard();
    password.value = "";
    if (!cancelled.signal.aborted && form.dataset.state !== "complete") enable(true);
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
  if (!cancelled.signal.aborted) {
    show("ready", "Use the synthetic web account for this test.");
    enable(true);
  }
} catch {
  show("unavailable", "Named sign-in is unavailable.");
}
