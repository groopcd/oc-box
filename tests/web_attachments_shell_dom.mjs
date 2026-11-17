// Real common-shell/addon scripts with synthetic browser objects. Not browser E2E.
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { File } from "node:buffer";
import { createRequire } from "node:module";
const addon = createRequire(import.meta.url)("../src/oc_box/web_attachments.js");
const source = fs.readFileSync(new URL("../src/oc_box/web_sessions.js", import.meta.url), "utf8");
const origin = "http://127.0.0.1:23456";
const prefix = "/server/c3ludGhldGlj/session/";
const rows = [1, 2, 3, 4].map(i => ({ id: `ses_${i}`, title: `Synthetic ${i}`, route: `${prefix}ses_${i}` }));
const child = { id: "ses_child", title: "Synthetic exact child", route: `${prefix}ses_child`, activeRoot: "ses_2" };
const png = Uint8Array.of(137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 0);
const image = () => new File([png], "screenshot.png", { type: "image/png" });
class Event {
  constructor(type, data = {}) { Object.assign(this, { type, isTrusted: false }, data); }
  preventDefault() { this.defaultPrevented = true; }
  stopImmediatePropagation() { this.stopped = true; }
}
class Events {
  listeners = new Map();
  addEventListener(type, fn, capture = false) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push({ fn, capture });
  }
  removeEventListener(type, fn) {
    this.listeners.set(type, (this.listeners.get(type) || []).filter(value => value.fn !== fn));
  }
  dispatchEvent(event) {
    event.target ??= this;
    for (const value of [...(this.listeners.get(event.type) || [])].sort((a, b) => Number(b.capture) - Number(a.capture))) {
      if (event.stopped) break;
      value.fn(event);
    }
    return !event.defaultPrevented;
  }
}
class Element extends Events {
  children = [];
  attrs = new Map();
  hidden = false;
  textContent = "";
  writes = 0;
  constructor(tag) { super(); this.tag = tag; }
  set innerHTML(_) { throw Error("unsafe/private DOM write"); }
  setAttribute(name, value) {
    this.attrs.set(name, value);
    if (name === "src") {
      this.writes++;
      this.contentWindow.location = new URL(value, origin);
    }
  }
  getAttribute(name) { return this.attrs.get(name) ?? null; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  click() { this.dispatchEvent(new Event("click", { isTrusted: true })); }
}
class Transfer {
  files = [];
  types = [];
  strings = {};
  items = { add: file => { this.files.push(file); this.types = ["Files"]; } };
  getData(type) { return this.strings[type] || ""; }
}
function nativeDocument(received) {
  const document = new Events();
  document.querySelector = () => { throw Error("native private widget lookup"); };
  document.dispatchEvent = function(event) {
    Events.prototype.dispatchEvent.call(this, event);
    if (!event.stopped) received.push([...event.dataTransfer.files]);
    return true;
  };
  return document;
}
async function settle() {
  await new Promise(resolve => setImmediate(resolve));
  for (let i = 0; i < 12; i++) await Promise.resolve();
}
async function fixture(version = "2.2.6") {
  const ids = ["web-shell", "session-tabs", "native", "retained-selection", "selection-status",
    "catalog-status", "no-selection", "refresh-tabs", "session-controls", "addon-status"];
  const elements = Object.fromEntries(ids.map(id => [id, new Element(id)]));
  const frame = elements.native;
  const received = [];
  const realm = new Events();
  Object.assign(realm, { File, DataTransfer: Transfer, DragEvent: Event,
    document: nativeDocument(received), location: new URL(rows[1].route, origin) });
  frame.contentWindow = realm;
  frame.attrs.set("src", rows[1].route);
  Object.defineProperty(frame, "contentDocument", { get() { throw Error("use standard document events, not native widgets"); } });
  elements["web-shell"].dataset = { bootstrap: JSON.stringify({
    status: "valid", nativePrefix: prefix, sessions: rows,
    selection: { ...rows[1], member: true, origin: "Launcher-declared, not native focus" },
    selectionNotice: "No native focus proof"
  }) };
  const window = new Events();
  window.location = new URL("/auth/oc-box-sessions", origin);
  window.history = { replaceState(_state, _title, url) { window.location = new URL(url, origin); } };
  let interval;
  window.setInterval = fn => { interval = fn; return 1; };
  window.clearInterval = () => {};
  for (const name of ["indexedDB", "localStorage", "sessionStorage"])
    Object.defineProperty(window, name, { get() { throw Error("private browser store"); } });
  const document = {
    hidden: false, getElementById: id => elements[id],
    createElement(tag) {
      assert.notEqual(tag, "iframe", "addon must not create another iframe");
      return new Element(tag);
    }
  };
  const calls = [];
  const fetch = async (url, options) => {
    calls.push(url);
    assert.equal(options.body, undefined);
    assert.equal(options.method, undefined); // GET only; no prompt API.
    if (url === "/api/info") return { ok: true, json: async () => ({ version }) };
    if (url.includes("/selection?")) {
      const id = new URL(url, origin).searchParams.get("session");
      const item = [...rows, child].find(row => row.id === id);
      return { ok: !!item, json: async () => item };
    }
    assert.equal(url, "/auth/oc-box-sessions/catalog");
    return { ok: true, json: async () => ({ status: "valid", sessions: rows }) };
  };
  vm.runInNewContext(source, { window, document, fetch, URL, CustomEvent: Event });
  await addon.boot(window, document, fetch);
  await settle();
  const controls = elements["session-controls"].children[0];
  const input = controls.children[1].children[0];
  const fileDrop = (file, target = realm.document) => {
    const data = new Transfer();
    data.items.add(file);
    data.strings["text/plain"] = "file:///host/screenshot.png";
    realm.document.dispatchEvent(new Event("drop", { dataTransfer: data, isTrusted: true, target }));
  };
  const load = () => { frame.dispatchEvent(new Event("load")); };
  return { elements, frame, realm, window, received, calls, input, fileDrop, load,
    tick: async () => { interval(); await settle(); } };
}
async function pending(f) {
  const file = image();
  let read, release;
  const started = new Promise(resolve => { read = resolve; });
  const data = file.arrayBuffer.bind(file);
  file.arrayBuffer = async () => {
    read();
    await new Promise(resolve => { release = resolve; });
    return data();
  };
  f.fileDrop(file);
  await started;
  return async () => { release(); await settle(); };
}
const f = await fixture();
assert.equal(f.window.ocBoxWebShell.frame, f.frame);
assert.equal(f.frame.writes, 0);
assert.equal(f.elements["session-tabs"].children.length, 4);
assert.equal(f.input.disabled, false);
f.fileDrop(image());
await settle();
assert.equal(f.received.length, 1);
assert.equal(f.received[0][0].type, "image/png");

// Public shell events invalidate pending bytes, including A -> B -> A within
// one unchanged SPA document. A same-ID metadata refresh is conservative too.
for (const mode of ["same-id-event", "metadata", "spa-away-back", "document-load", "null-selection"]) {
  const g = await fixture();
  const release = await pending(g);
  if (mode === "same-id-event") g.window.dispatchEvent(new Event("oc-box:selection-changed",
    { detail: g.window.ocBoxWebShell.selection() }));
  if (mode === "metadata") await g.window.ocBoxWebShell.refresh();
  if (mode === "spa-away-back") {
    g.realm.location = new URL(rows[2].route, origin); await g.tick();
    g.realm.location = new URL(rows[1].route, origin); await g.tick();
    assert.equal(g.window.ocBoxWebShell.selection().sessionID, rows[1].id);
  }
  if (mode === "document-load") {
    g.realm.document = nativeDocument(g.received); g.load(); await settle();
  }
  if (mode === "null-selection") {
    g.realm.location = new URL("/", origin); await g.tick();
    assert.equal(g.window.ocBoxWebShell.selection(), null);
  }
  await release();
  assert.equal(g.received.length, 0, mode);
  assert.match(g.elements["addon-status"].textContent, /session changed/, mode);
}
{
  const g = await fixture();
  const release = await pending(g);
  g.realm.location = new URL(rows[3].route, origin);
  // No tick/load/selection event yet: public selection() returns null immediately.
  await release();
  assert.equal(g.received.length, 0);
  assert.match(g.elements["addon-status"].textContent, /session changed/);
}
{
  const g = await fixture();
  g.realm.location = new URL(rows[2].route, origin); await g.tick();
  const release = await pending(g);
  g.elements["session-tabs"].children[2].click();
  assert.equal(g.frame.writes, 0, "clicking the selected live SPA route must not reload");
  await release();
  assert.equal(g.received.length, 0, "same-ID explicit click still invalidates pending bytes");
  assert.match(g.elements["addon-status"].textContent, /session changed/);
}
{
  const g = await fixture();
  g.elements["session-tabs"].children[3].click(); g.load(); await settle();
  g.elements["session-tabs"].children[1].click(); g.load(); await settle();
  g.fileDrop(image()); await settle();
  assert.equal(g.received.length, 1, "no stale per-session capture handler");
  assert.equal(g.frame.writes, 2, "only common-shell tab choices write iframe src");
  g.input.files = [image()];
  g.input.dispatchEvent(new Event("change")); await settle();
  assert.equal(g.received.length, 1, "untrusted file input cannot authorize a drop");
  g.input.dispatchEvent(new Event("change", { isTrusted: true })); await settle();
  assert.equal(g.received.length, 2);
}
const refused = await fixture("2.2.7");
refused.fileDrop(image()); await settle();
assert.equal(refused.received.length, 0);
assert.equal(refused.input.disabled, true);
{
  const g = await fixture();
  g.realm.location = new URL(child.route, origin); await g.tick();
  const selected = g.window.ocBoxWebShell.selection();
  assert.equal(selected.sessionID, child.id);
  assert.equal(selected.nativeRoute, child.route);
  assert.equal(selected.activeRoot, "ses_2");
  assert.equal(selected.member, false, "an exact child is not fabricated as an open root");
  assert.equal(g.elements["session-tabs"].children.length, 4);
  assert.equal(g.elements["session-tabs"].children[1].getAttribute("aria-selected"), "true");
  const original = {
    ownerDocument: g.realm.document, isConnected: true,
    dispatchEvent(event) { event.target = original; return g.realm.document.dispatchEvent(event); },
  };
  g.fileDrop(image(), original); await settle();
  assert.equal(g.received.length, 1, "verified child accepts files despite root-only membership");
  assert.equal(g.realm.location.pathname, child.route, "activeRoot must never replace the exact attachment route");
  assert.equal(g.frame.writes, 0, "addon must not navigate from child to its highlighted root");
}
assert(f.calls.every(url => url === "/api/info" || url.startsWith("/auth/oc-box-sessions/")));
console.log("PASS shared-shell addon DOM: one iframe/four tabs, trusted exact child/root selection, ABA/generation/load/SPA refusal, no stores or prompt requests");
