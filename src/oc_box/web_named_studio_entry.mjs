// First-party entry for the authenticated named workspace document only.
import { createNamedStudioController } from "./web_named_studio_client.mjs";

const catalogPath = "/oc-box/studio/catalog";
const catalogLimit = 8192;
const sessionID = /^ses[A-Za-z0-9_-]{1,125}$/u;
const encoder = new TextEncoder();
const decoder = new TextDecoder("utf-8", { fatal: true });

async function catalog(signal) {
  const abort = new AbortController();
  const cancel = () => abort.abort();
  signal.addEventListener("abort", cancel, { once: true });
  if (signal.aborted) abort.abort();
  const timer = setTimeout(cancel, 7000);
  try {
    const url = location.origin + catalogPath;
    const response = await fetch(url, {
      mode: "same-origin", credentials: "same-origin", cache: "no-store",
      redirect: "error", referrerPolicy: "no-referrer", signal: abort.signal,
      headers: { Accept: "application/json" },
    });
    const length = response.headers.get("content-length");
    const size = Number(length);
    if (response.status !== 200 || response.url !== url || response.type !== "basic" ||
        response.redirected || response.headers.get("content-type") !== "application/json" ||
        !/^[1-9][0-9]{0,3}$/u.test(length ?? "") || size > catalogLimit ||
        !response.body || abort.signal.aborted) throw new TypeError();
    const bytes = new Uint8Array(size);
    const stream = response.body.getReader();
    let offset = 0;
    try {
      for (;;) {
        const { value, done } = await stream.read();
        if (abort.signal.aborted) throw new TypeError();
        if (done) break;
        if (!(value instanceof Uint8Array) || offset + value.byteLength > size) throw new TypeError();
        bytes.set(value, offset);
        offset += value.byteLength;
      }
    } finally { stream.releaseLock(); }
    if (offset !== size) throw new TypeError();
    const raw = decoder.decode(bytes);
    const data = JSON.parse(raw);
    // The host emits one compact, UTF-8 envelope. No duplicate fields,
    // credentials, arbitrary paths or a cursor may reach the renderer.
    if (JSON.stringify(data) !== raw || !data ||
        Reflect.ownKeys(data).length !== 2 || typeof data.hasEarlier !== "boolean" ||
        !Array.isArray(data.sessions) || data.sessions.length > 16) throw new TypeError();
    const ids = new Set();
    for (const row of data.sessions) {
      if (!row || Reflect.ownKeys(row).length !== 4 || typeof row.id !== "string" ||
          !sessionID.test(row.id) || ids.has(row.id) || typeof row.title !== "string" ||
          !row.title || encoder.encode(row.title).byteLength > 160 ||
          /[\p{Cc}\p{Cf}\p{Cs}]/u.test(row.title) ||
          typeof row.archived !== "boolean" || typeof row.child !== "boolean") throw new TypeError();
      ids.add(row.id);
    }
    return { status: "valid", sessions: data.sessions, hasEarlier: data.hasEarlier };
  } finally {
    clearTimeout(timer);
    signal.removeEventListener("abort", cancel);
    abort.abort();
  }
}

function mount() {
  const root = document.getElementById("web-shell");
  if (!root?.hasAttribute("data-named-studio") || !root.hasAttribute("data-studio-page") ||
      typeof window.ocBoxMountNamedStudioChrome !== "function") return;
  const bootstrap = JSON.parse(root.dataset.studioPage);
  delete root.dataset.studioPage;
  let client = null;
  const chrome = window.ocBoxMountNamedStudioChrome({
    async verifySelection(id, { event, signal }) {
      if (!client || signal.aborted) return false;
      // choose() consumes the live, explicit row click synchronously.
      const selected = await client.choose(event, id);
      if (signal.aborted) { client.invalidate(); return false; }
      return selected;
    },
    selectionChanged(selection) {
      if (selection === null) client?.invalidate();
      else if (client) void client.present();
    },
    readCatalog({ signal }) {
      if (!client) throw new TypeError();
      return catalog(signal);
    },
  });
  if (!chrome) return;
  client = createNamedStudioController(root, bootstrap);
  if (client) void chrome.refresh(); // Metadata only; never choose or Send.
}

function start() {
  try { mount(); }
  catch { /* Leave the first-party document in its honest unavailable state. */ }
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true });
else start();
