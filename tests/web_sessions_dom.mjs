import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { addStudioControls } from "./web_studio_dom_fixture.mjs";

const source = fs.readFileSync(new URL("../src/oc_box/web_sessions.js", import.meta.url), "utf8");
const origin = "http://127.0.0.1:23456";
const prefix = "/server/c3ludGhldGlj/session/";
const item = id => ({ id: `ses_${id}`, title: `<img onerror=bad> ${id}`, route: `${prefix}ses_${id}` });
const rows = [1, 2, 3, 4].map(item);
class Events {
  listeners = new Map();
  addEventListener(name, fn) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(fn);
  }
  removeEventListener(name, fn) {
    this.listeners.set(name, (this.listeners.get(name) || []).filter(value => value !== fn));
  }
  dispatchEvent(event) {
    for (const fn of this.listeners.get(event.type) || []) fn(event);
  }
}
class Element extends Events {
  constructor(id) {
    super(); this.id = id; this.attrs = new Map(); this.children = [];
    this.hidden = false; this.textContent = ""; this.writes = 0;
    this.dataset = {};
  }
  set innerHTML(_) { throw Error("unsafe HTML"); }
  setAttribute(name, value) {
    this.attrs.set(name, value);
    if (name === "src") {
      this.writes += 1;
      this.contentWindow.location = new URL(value, origin);
      this.draft = null;
    }
  }
  getAttribute(name) { return this.attrs.get(name) ?? null; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  click() { this.dispatchEvent({ type: "click" }); }
}
function fixture(initial = rows[1], membershipStatus = "valid") {
  const ids = ["web-shell", "session-tabs", "native", "retained-selection",
    "selection-status", "catalog-status", "no-selection", "refresh-tabs",
    "session-controls", "addon-status"];
  const elements = Object.fromEntries(ids.map(id => [id, new Element(id)]));
  const frame = elements.native;
  frame.contentWindow = new Events();
  frame.contentWindow.location = new URL(initial?.route || "about:blank", origin);
  Object.defineProperty(frame, "contentDocument", { get() { throw Error("private DOM"); } });
  if (initial) frame.attrs.set("src", initial.route);
  frame.hidden = !initial;
  frame.draft = "synthetic unsent draft";
  elements["web-shell"].dataset = { bootstrap: JSON.stringify({
    status: membershipStatus, nativePrefix: prefix,
    sessions: membershipStatus === "valid" ? rows : [],
    selection: initial ? { ...initial, origin: "Launcher-declared; not native focus" } : null,
    selectionNotice: "No native focus evidence"
  }) };
  const window = new Events();
  addStudioControls(elements, id => new Element(id), window);
  window.location = new URL("/auth/oc-box-sessions", origin);
  window.history = {
    replaceState(_state, _title, url) { window.location = new URL(url, origin); }
  };
  let interval;
  window.setInterval = (fn, delay) => { assert.equal(delay, 1000); interval = fn; return 1; };
  window.clearInterval = () => { interval = null; };
  for (const name of ["indexedDB", "localStorage", "sessionStorage"])
    Object.defineProperty(window, name, { get() { throw Error("private store"); } });
  let nextCatalog = rows;
  let pending = null;
  const calls = [];
  let active = 0;
  let maximum = 0;
  const fetch = async (url, options) => {
    calls.push(url);
    assert.equal(options.credentials, "same-origin");
    assert.equal(options.method, undefined); // GET only.
    assert.equal(options.body, undefined);
    active += 1; maximum = Math.max(active, maximum);
    try {
      if (pending) await pending;
      if (url.includes("/selection?")) {
        const id = new URL(url, origin).searchParams.get("session");
        const row = [1, 2, 3, 4, 5, 6].map(item).find(value => value.id === id);
        return { ok: !!row, json: async () => row };
      }
      assert.equal(url, "/auth/oc-box-sessions/catalog");
      return { ok: true, json: async () => ({ sessions: nextCatalog, status: "valid" }) };
    } finally { active -= 1; }
  };
  const document = {
    hidden: false,
    getElementById: id => elements[id],
    createElementNS: (_namespace, tag) => new Element(tag),
    createElement: tag => {
      assert.notEqual(tag, "iframe");
      return new Element(tag);
    }
  };
  vm.runInNewContext(source, {
    document, window, URL, fetch,
    CustomEvent: class { constructor(type, data) { this.type = type; this.detail = data.detail; } }
  });
  return {
    elements, frame, window, calls,
    tick: () => interval?.(),
    catalog: rows => { nextCatalog = rows; },
    defer: promise => { pending = promise; },
    maximum: () => maximum
  };
}
async function settle() {
  for (let i = 0; i < 12; i++) await Promise.resolve();
}
const f = fixture();
const api = f.window.ocBoxWebShell;
assert.equal(api.version, 1);
assert.equal(api.frame, f.frame);
assert.equal(f.frame.writes, 0); // No startup iframe reload.
assert.equal(f.elements["session-tabs"].children.length, 4);
assert.equal(f.elements["session-tabs"].children[1].getAttribute("aria-selected"), "true");
assert.equal(api.selection().sessionID, "ses_2");
assert.equal(f.elements["session-tabs"].children[0].children[1].children[0].textContent, rows[0].title);
f.elements["session-tabs"].children[3].click();
assert.equal(f.frame.getAttribute("src"), rows[3].route);
assert.equal(f.window.location.searchParams.get("session"), "ses_4");
assert.equal(api.selection(), null); // No addon binding until pending load completes.
f.frame.dispatchEvent({ type: "load" });
await settle();
assert.equal(api.selection().sessionID, "ses_4");
assert.equal(f.frame.writes, 1);
f.frame.draft = "keep this unsent draft";
f.catalog([rows[0], rows[1], rows[2], item(5)]);
await api.refresh();
assert.equal(f.frame.writes, 1);
assert.equal(f.frame.draft, "keep this unsent draft");
assert.equal(api.selection().member, false);
assert.equal(f.elements["session-tabs"].children.length, 4);
assert.match(f.elements["retained-selection"].children[0].textContent, /not counted as TUI-open/);

// Native pushState-style SPA navigation does not fire iframe.load.
f.frame.contentWindow.location = new URL(rows[1].route, origin);
f.tick(); await settle();
assert.equal(api.selection().sessionID, "ses_2");
assert.equal(f.frame.writes, 1);
assert.equal(f.window.location.searchParams.get("session"), "ses_2");
assert.equal(f.elements["session-tabs"].children[1].getAttribute("aria-selected"), "true");
// The src attribute still names the previous shell navigation. Clicking the
// already-selected live SPA route must not reload that conversation's draft.
assert.notEqual(f.frame.getAttribute("src"), rows[1].route);
f.frame.draft = "native SPA draft must remain";
f.elements["session-tabs"].children[1].click();
assert.equal(f.frame.writes, 1);
assert.equal(f.frame.draft, "native SPA draft must remain");
assert.equal(api.selection().sessionID, "ses_2");
f.frame.contentWindow.location = new URL(item(6).route, origin);
f.tick(); await settle();
assert.equal(api.selection().sessionID, "ses_6");
assert.equal(api.selection().member, false);
assert.equal(f.elements["session-tabs"].children.length, 4);
for (const path of ["/", "/new-session?draftId=synthetic", "/server/foreign/session/ses_1",
                    "https://foreign.invalid/session/ses_1"]) {
  const count = f.calls.length;
  f.frame.contentWindow.location = new URL(path, origin);
  f.tick(); await settle();
  assert.equal(api.selection(), null);
  assert.equal(f.window.location.searchParams.has("session"), false);
  assert.equal(f.frame.writes, 1);
  assert.equal(f.calls.length, count);
  assert(f.elements["session-tabs"].children.every(
    button => button.getAttribute("aria-selected") === "false"
  ));
}
let release;
f.defer(new Promise(resolve => { release = resolve; }));
const refresh1 = api.refresh();
const refresh2 = api.refresh();
f.frame.contentWindow.location = new URL(rows[0].route, origin);
f.tick();
release(); await Promise.all([refresh1, refresh2]); await settle();
f.defer(null);
f.tick(); await settle();
assert.equal(f.maximum(), 1);
assert.equal(api.selection().sessionID, "ses_1");
assert.equal(f.frame.writes, 1);
assert(f.calls.every(url => url.startsWith("/auth/oc-box-sessions/")));

const empty = fixture(null);
assert.equal(empty.window.ocBoxWebShell.selection(), null);
assert.equal(empty.frame.writes, 0);
assert.equal(empty.frame.hidden, true);
assert(empty.elements["session-tabs"].children.every(
  button => button.getAttribute("aria-selected") === "false"
));
const unknown = fixture(rows[1], "unavailable");
assert.equal(unknown.window.ocBoxWebShell.selection().sessionID, "ses_2");
assert.equal(unknown.window.ocBoxWebShell.selection().member, false);
assert.equal(unknown.elements["session-tabs"].children.length, 0);
assert.match(unknown.elements["catalog-status"].textContent, /count unknown/);
assert.equal(unknown.frame.writes, 0);
const child = fixture({ ...item(5), activeRoot: "ses_2" });
assert.equal(child.window.ocBoxWebShell.selection().sessionID, "ses_5");
assert.equal(child.window.ocBoxWebShell.selection().activeRoot, "ses_2");
assert.equal(child.elements["session-tabs"].children.length, 4);
assert.equal(child.elements["session-tabs"].children[1].getAttribute("aria-selected"), "true");
assert.match(child.elements["retained-selection"].children[0].textContent, /Child conversation/);
assert.equal(child.frame.writes, 0);
child.catalog([rows[0], rows[2], rows[3]]);
await child.window.ocBoxWebShell.refresh();
assert.equal(child.window.ocBoxWebShell.selection().sessionID, "ses_5");
assert.equal(child.window.ocBoxWebShell.selection().activeRoot, null);
assert.equal(child.elements["session-tabs"].children.length, 3);
assert.match(child.elements["retained-selection"].children[0].textContent, /not counted as TUI-open/);
assert.equal(child.frame.writes, 0);
console.log("PASS common shell DOM: four tabs, explicit selection, retained draft, verified SPA navigation, foreign/home/draft unbinding, one iframe and non-overlapping GETs.");
