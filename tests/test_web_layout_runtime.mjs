// Offline shell contract, not interactive browser/native or host acceptance.
import assert from "node:assert/strict";
import { File } from "node:buffer";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const repo = fileURLToPath(new URL("../", import.meta.url));
const source = fs.readFileSync(new URL("../src/oc_box/web_sessions.js", import.meta.url), "utf8");
const { adapter } = createRequire(import.meta.url)("../src/oc_box/web_attachments.js");
const { elements: markup, layouts, sections } = JSON.parse(execFileSync("python3", ["-B", "-c", `
from html.parser import HTMLParser
import json
from types import SimpleNamespace as Namespace
from unittest.mock import patch

from oc_box import web_sessions as web

class Parsed(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.elements, self.frames, self.scripts, self.links = {}, [], [], []
        self.trail, self.parents = [], {}
        self.layouts, self.sections = [], []
        self.button = None
        self.feed(body.decode())

    def handle_starttag(self, tag, pairs):
        attrs = dict(pairs)
        if "id" in attrs:
            assert attrs["id"] not in self.elements
            self.elements[attrs["id"]] = attrs
            self.parents[attrs["id"]] = [ident for _, ident in self.trail if ident]
        if tag not in ("meta", "link", "input"):
            self.trail.append((tag, attrs.get("id")))
        if tag == "iframe":
            self.frames.append(attrs)
        if tag == "script":
            self.scripts.append(attrs)
        if tag == "a":
            self.links.append(attrs)
        if tag == "button":
            for field, group in (("data-view", self.layouts), ("data-section", self.sections)):
                if field in attrs:
                    self.button = {"id": attrs[field], "text": ""}
                    group.append(self.button)
        assert tag not in ("form", "textarea")

    def handle_endtag(self, tag):
        assert self.trail.pop()[0] == tag
        if tag == "button":
            if self.button is not None:
                self.button["text"] = self.button["text"].strip()
            self.button = None

    def handle_data(self, data):
        if self.button is not None:
            self.button["text"] += data

gateway = Namespace(
    box=Namespace(BoxError=RuntimeError), url="http://127.0.0.1:4242",
    data={"project": "/host/no-browser-disclosure", "container_project": "/retained/Studio <example>"},
    connection={"session": "ses_alpha"}, shell_addon_scripts=("/auth/oc-box-attachments.js",),
)
rows = [
    {"id": "ses_alpha", "title": "Alpha <button>"},
    {"id": "ses_bravo", "title": "Bravo workspace"},
]
def response(query="", *, authenticated=True):
    replies = []
    request = f"GET {web.INDEX}{query} HTTP/1.1\\r\\nHost: 127.0.0.1:4242\\r\\n\\r\\n".encode()
    with (patch.object(web, "_authenticated", return_value=authenticated),
          patch.object(web, "reported_open_sessions", return_value=(rows, "valid"))):
        assert web.respond(gateway, Namespace(sendall=replies.append), None, request)
    headers, body = replies[0].split(b"\\r\\n\\r\\n", 1)
    assert b"Content-Security-Policy: default-src 'none';" in headers
    return headers, body

assert response("?layout=compact")[0].startswith(b"HTTP/1.1 503 ")
assert response(authenticated=False)[0].startswith(b"HTTP/1.1 401 ")
headers, body = response()
assert headers.startswith(b"HTTP/1.1 200 ")
layout = Parsed(body)
assert layout.frames == [layout.elements["native"]]
assert layout.scripts == [
    {"defer": None, "src": web.SCRIPT}, {"defer": None, "src": "/auth/oc-box-attachments.js"},
]
assert {"href": "/", "target": "_blank", "rel": "noopener noreferrer"} in layout.links
assert layout.elements["web-shell"]["data-layout"] == "studio"
assert layout.elements["workspace-search"]["type"] == "search"
assert layout.elements["workspace-manager-nav"].get("hidden", False) is None
def parent(element, expected):
    assert layout.parents[element][-1] == expected, layout.parents[element]
parent("workspace-toolbar", "workspace-masthead")
parent("workspace-review", "workspace-masthead")
parent("workspace-layouts", "workspace-review")
parent("workspace-library", "workspace-content")
parent("workspace-open-group", "workspace-library")
parent("workspace-history-group", "workspace-library")
parent("workspace-archived-group", "workspace-library")
parent("session-tabs", "workspace-open-group")
parent("workspace-inspector-toggle", "workspace-chat")
parent("native", "workspace-chat")
parent("workspace-inspector-close", "workspace-overview")
parent("selection-status", "workspace-overview")
assert "hidden" in layout.elements["workspace-history-group"]
assert "hidden" in layout.elements["workspace-archived-group"]
assert layout.elements["i-box"] and layout.elements["i-user"] and layout.elements["i-grid"]
assert [item["id"] for item in layout.sections] == ["status", "sessions"]
assert b"Studio &lt;example&gt;" in body and b"Workspace guest" in body
assert all(value not in body for value in (b"/host/", b"no-browser-disclosure", b"/retained/", b"Full TUI", b"Preview sign-in"))
assert b"No conversation is guessed." in body
print(json.dumps({"elements": layout.elements, "layouts": layout.layouts, "sections": layout.sections}))
`], {
  cwd: repo, env: { ...process.env, PYTHONPATH: repo + "src" }, encoding: "utf8", timeout: 15_000,
}));

class Events {
  handlers = new Map();
  addEventListener(name, handler) {
    this.handlers.set(name, [...(this.handlers.get(name) || []), handler]);
  }
  removeEventListener(name, handler) {
    this.handlers.set(name, (this.handlers.get(name) || []).filter(fn => fn !== handler));
  }
  dispatchEvent(event) {
    for (const fn of this.handlers.get(event.type) || []) fn(event);
  }
  emit(type, details = {}) {
    const event = { type, preventDefault() { this.defaultPrevented = true; }, ...details };
    this.dispatchEvent(event);
    return event;
  }
}
class Element extends Events {
  constructor(attrs = {}) {
    super();
    this.attrs = new Map(Object.entries(attrs));
    this.hidden = Object.hasOwn(attrs, "hidden");
    this.dataset = {};
    this.style = new Map();
    this.style.setProperty = this.style.set;
    this.children = [];
    this.textContent = this.value = "";
    this.srcWrites = 0;
  }
  set innerHTML(_) { assert.fail("unsafe HTML"); }
  setAttribute(attr, value) {
    this.attrs.set(attr, value);
    if (attr === "src") {
      this.srcWrites++;
      this.contentWindow.location = new URL(value, origin);
      this.draft = null;
    }
  }
  getAttribute(attr) { return this.attrs.get(attr); }
  querySelectorAll(selector) {
    assert.equal(selector, "button");
    return this.children;
  }
  focus() { this.focused = true; }
  click() { this.emit("click"); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setPointerCapture(id) { this.capture = id; }
  hasPointerCapture(id) { return this.capture === id; }
  releasePointerCapture(id) { if (this.hasPointerCapture(id)) this.capture = null; }
}

const origin = "http://127.0.0.1:4242";
const bootstrap = JSON.parse(markup["web-shell"]["data-bootstrap"]);
const [alpha, bravo] = bootstrap.sessions;
function boot({ failInitialRoute = false, initialWidth = 1200 } = {}) {
  const nodes = Object.fromEntries(Object.entries(markup).map(([key, attrs]) => [key, new Element(attrs)]));
  const get = id => nodes[id];
  const root = get("web-shell");
  const frame = get("native");
  root.dataset = { bootstrap: JSON.stringify(bootstrap), layout: markup["web-shell"]["data-layout"] };
  frame.contentWindow = new Events();
  frame.contentWindow.location = new URL(frame.getAttribute("src"), origin);
  frame.draft = "synthetic unsent draft";
  Object.defineProperty(frame, "contentDocument", { get() { assert.fail("shell read native DOM"); } });
  for (const [nav, key, data] of [["layouts", "view", layouts], ["manager-nav", "section", sections]]) {
    get("workspace-" + nav).children = data.map(({ id, text }) => {
      const button = new Element();
      button.dataset[key] = id;
      button.textContent = text;
      return button;
    });
  }
  const switchView = view => get("workspace-layouts").children.find(b => b.dataset.view === view).click();
  const showSection = section => get("workspace-manager-nav").children.find(b => b.dataset.section === section).click();
  const document = {
    getElementById: get,
    createElementNS() { return new Element(); },
    createElement(tag) { assert.notEqual(tag, "iframe"); return new Element(); },
  };
  const window = new Events();
  window.location = new URL("/auth/oc-box-sessions", origin);
  window.history = { replaceState(_state, _title, url) {
    if (failInitialRoute) throw new Error("Synthetic history API refusal");
    window.location = new URL(url, origin);
  } };
  let observe;
  window.setInterval = fn => { observe = fn; return 1; };
  for (const name of ["localStorage", "indexedDB", "sessionStorage"])
    Object.defineProperty(window, name, { get() { assert.fail("shell read browser state"); } });
  const wide = new Events(), roomy = new Events();
  window.matchMedia = query => {
    if (query === "(min-width: 1001px)") return wide;
    assert.equal(query, "(min-width: 681px)");
    return roomy;
  };
  function resize(width, desktop) {
    get("workspace-content").clientWidth = width;
    const changedWide = wide.matches !== desktop;
    wide.matches = desktop;
    const changed = roomy.matches !== (width >= 681);
    roomy.matches = width >= 681;
    if (changed) roomy.emit("change");
    if (changedWide) wide.emit("change");
    window.emit("resize");
  }
  resize(initialWidth, initialWidth >= 1001);
  const generations = [];
  window.addEventListener("oc-box:selection-changed", event => generations.push(event.detail?.sessionID ?? null));
  const network = { calls: [], catalog: bootstrap, selected: bravo, pending: null, history: null };
  const fetch = async (url, options) => {
    network.calls.push(url);
    assert.equal(options.method, undefined);
    assert.equal(options.credentials, "same-origin");
    assert.equal(options.body, undefined);
    if (network.pending) await network.pending;
    if (url === "/auth/oc-box-sessions/catalog")
      return { ok: !!network.catalog, json: async () => network.catalog };
    if (url === "/auth/oc-box-sessions/history")
      return { ok: !!network.history, json: async () => network.history };
    if (url === "/auth/oc-box-sessions/history?session=ses_bravo")
      return { ok: !!network.history?.[0], json: async () => network.history[0] };
    assert.equal(url, "/auth/oc-box-sessions/selection?session=ses_bravo");
    return { ok: !!network.selected, json: async () => network.selected };
  };
  vm.runInNewContext(source, { document, window, URL, fetch, AbortController, CustomEvent: class {
    constructor(type, options) { Object.assign(this, { type, ...options }); }
  } });
  return { get, root, frame, window, switchView, showSection, resize, generations, network,
    shell: window.ocBoxWebShell, tick: () => observe() };
}
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const rowTitle = row => row.children[1].children[0].textContent;
const rowDetails = row => row.children[1].children[1].textContent;

{
  const f = boot({ failInitialRoute: true, initialWidth: 390 });
  assert.equal(f.shell, undefined);
  assert.equal(f.get("workspace-library").hidden, false);
  assert.equal(f.get("fallback-links").hidden, false);
  assert.match(f.get("catalog-status").textContent, /explicit fallback links or native Home/);
  f.resize(1200, true);
  f.resize(390, false);
  f.switchView("manager");
  assert.equal(f.root.dataset.layout, "studio");
  assert.equal(f.get("workspace-library").hidden, false);
  assert.equal(f.frame.srcWrites, 0);
  assert.deepEqual(f.network.calls, []);
}

{
  const f = boot();
  const { get, frame } = f;
  f.network.history = [{
    id: "ses_bravo", title: "An archived child", isChild: true,
    time: { created: 1, updated: 2, archived: 2 },
  }, {
    id: "ses_history_only", title: "A non-archived session", isChild: false,
    time: { created: 2, updated: 3 },
  }];
  assert.deepEqual(f.network.calls, []);
  get("workspace-history-tabs").click();
  await settle();
  assert.deepEqual(f.network.calls, ["/auth/oc-box-sessions/history"]);
  assert.match(get("workspace-search-status").textContent, /not TUI membership/i);
  assert.equal(get("workspace-open-group").hidden, true);
  assert.equal(get("workspace-history-group").hidden, false);
  assert.equal(get("workspace-archived-group").hidden, false);
  assert.equal(rowTitle(get("workspace-history-list").children[0]), "A non-archived session");
  get("workspace-archived-tabs").click();
  assert.equal(get("workspace-history-group").hidden, true);
  assert.equal(get("workspace-archived-tabs").getAttribute("aria-pressed"), "true");
  assert.deepEqual(f.network.calls, ["/auth/oc-box-sessions/history"]);
  const historyRow = get("workspace-archived-list").children[0];
  assert.equal(rowTitle(historyRow), "An archived child");
  assert.equal(rowDetails(historyRow), "Child session · Archived · In open TUI tabs");
  assert.equal(historyRow.children[0].children[0].getAttribute("href"), "#i-child");
  assert.equal(frame.srcWrites, 0);
  let release;
  f.network.pending = new Promise(resolve => { release = resolve; });
  historyRow.click();
  get("workspace-open-tabs").click();
  release();
  await settle();
  assert.equal(frame.srcWrites, 0); // A changed source invalidates the in-flight selection.
  get("workspace-history-tabs").click();
  f.switchView("manager");
  f.showSection("sessions");
  f.network.pending = new Promise(resolve => { release = resolve; });
  get("workspace-archived-list").children[0].click();
  f.showSection("status");
  release();
  await settle();
  assert.equal(frame.srcWrites, 0);
  assert.equal(f.root.dataset.layout, "manager");
  f.showSection("sessions");
  get("workspace-archived-list").children[0].click();
  await settle();
  assert.equal(frame.srcWrites, 1);
  assert.equal(frame.getAttribute("src"), bravo.route);
  assert.equal(get("workspace-conversation-source").textContent, "OpenCode / Archived in workspace");
  assert.equal(f.network.calls[1], "/auth/oc-box-sessions/history?session=ses_bravo");
}

{
  const f = boot();
  const { root, frame, shell, get, generations } = f;
  assert.equal(shell.frame, frame);
  assert.equal(shell.controls, get("session-controls"));
  assert.equal(shell.status, get("addon-status"));
  assert.equal(shell.selection().sessionID, alpha.id);
  assert.equal(root.dataset.layout, "studio");
  assert.equal(frame.srcWrites, 0);
  f.switchView("dashboard");
  assert.equal(root.dataset.layout, "dashboard");
  assert.equal(get("workspace-overview").hidden, false);
  f.switchView("focus");
  assert.equal(get("workspace-overview").hidden, true);
  assert.equal(get("workspace-library").hidden, true);
  const toggle = get("workspace-sessions-toggle");
  assert.equal(toggle.hidden, false);
  toggle.click();
  assert.equal(get("workspace-library").hidden, false);
  assert.equal(toggle.getAttribute("aria-expanded"), "true");
  root.emit("keydown", { key: "Escape" });
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  f.switchView("manager");
  assert.equal(shell.selection(), null);
  assert.equal(get("workspace-chat").hidden, true);
  assert.equal(get("workspace-overview").hidden, false);
  assert.match(get("catalog-status").textContent, /2 verified open/);
  f.showSection("sessions");
  assert.equal(get("workspace-overview").hidden, true);
  assert.equal(get("workspace-library").hidden, false);
  assert.equal(get("fallback-links").hidden, true);
  const query = get("workspace-search");
  const rows = () => get("session-tabs").children;
  const search = string => { query.value = string; query.emit("input"); };
  search(" workspace ");
  assert.deepEqual(rows().map(rowTitle), [bravo.title]);
  search("SES_ALPHA");
  assert.deepEqual(rows().map(rowTitle), [alpha.title]);
  search("not an open session");
  assert.deepEqual(rows(), []);
  assert.match(get("workspace-search-status").textContent, /no .*match/i);
  assert.deepEqual(f.network.calls, []);
  f.network.catalog = null;
  await shell.refresh();
  search("");
  assert.deepEqual(rows(), []); // Never offer stale rows as verified in the manager.
  assert.match(get("workspace-search-status").textContent, /unavailable/);
  f.network.catalog = { ...bootstrap, sessions: [] };
  await shell.refresh();
  assert.match(get("workspace-search-status").textContent, /No verified open/);
  f.network.catalog = bootstrap;
  await shell.refresh();
  assert.equal(rows()[0].getAttribute("aria-label"), "Open in native chat: " + alpha.title);
  rows()[0].click(); // Return from manager to the same native draft.
  assert.equal(get("workspace-chat").hidden, false);
  assert.equal(root.dataset.layout, "focus");
  assert.equal(shell.selection().sessionID, alpha.id);
  f.switchView("studio");
  assert.deepEqual(generations.slice(0, 4), [alpha.id, alpha.id, alpha.id, null]);
  assert.deepEqual(generations.slice(-2), [alpha.id, alpha.id]);
  assert.equal(frame.srcWrites, 0);
  assert.equal(frame.draft, "synthetic unsent draft");
}

{
  const f = boot();
  const { get, frame } = f;
  f.resize(390, false);
  const sessions = get("workspace-sessions-toggle");
  const inspector = get("workspace-inspector-toggle");
  assert.equal(get("workspace-chat").hidden, false);
  assert.equal(get("workspace-library").hidden, true);
  assert.equal(get("workspace-overview").hidden, true);
  sessions.click();
  assert.equal(sessions.getAttribute("aria-expanded"), "true");
  assert.equal(get("workspace-search").focused, true);
  inspector.click();
  assert.equal(sessions.getAttribute("aria-expanded"), "false");
  assert.equal(inspector.getAttribute("aria-expanded"), "true");
  assert.equal(get("workspace-library").hidden, true);
  assert.equal(get("workspace-overview-title").focused, true);
  assert.equal(get("workspace-chat").inert, true);
  f.root.emit("keydown", { key: "Escape", defaultPrevented: true });
  assert.equal(inspector.getAttribute("aria-expanded"), "true");
  f.root.emit("keydown", { key: "Escape" });
  assert.equal(inspector.getAttribute("aria-expanded"), "false");
  assert.equal(inspector.focused, true);
  assert.equal(get("workspace-chat").inert, false);
  f.resize(1200, true);
  assert.equal(inspector.hidden, false);
  assert.equal(get("workspace-overview").hidden, false);
  get("workspace-inspector-close").click();
  assert.equal(inspector.getAttribute("aria-expanded"), "false");
  assert.equal(get("workspace-divider-right").hidden, true);
  get("workspace-inspector-link").click();
  assert.equal(inspector.getAttribute("aria-expanded"), "true");
  assert.equal(get("workspace-divider-right").hidden, false);
  f.resize(900, false);
  assert.equal(get("workspace-library").hidden, false);
  assert.equal(get("workspace-overview").hidden, true);
  assert.equal(frame.srcWrites, 0);
  assert.equal(frame.draft, "synthetic unsent draft");
  assert.deepEqual(f.network.calls, []);
}

{
  const f = boot();
  let release;
  f.network.pending = new Promise(resolve => { release = resolve; });
  f.frame.contentWindow.location = new URL(bravo.route, origin); // Public SPA route, no iframe load.
  f.tick();
  f.switchView("manager");
  f.tick();
  assert.deepEqual(f.network.calls, ["/auth/oc-box-sessions/selection?session=ses_bravo"]);
  f.switchView("studio");
  release();
  await settle();
  assert.equal(f.shell.selection(), null); // In-flight verification predates layout change.
  f.tick();
  await settle();
  assert.equal(f.shell.selection().sessionID, bravo.id);
  assert.equal(f.frame.srcWrites, 0);
  assert.equal(f.frame.draft, "synthetic unsent draft");
}

{
  // The real add-on's generation must invalidate a File read even when the
  // manager visit ends on the exact same frame, URL, document and session ID.
  const f = boot();
  const realm = f.frame.contentWindow;
  realm.document = new Events();
  realm.File = File;
  let forwarded = 0;
  realm.DataTransfer = class { constructor() { forwarded += 1; } };
  const notices = [];
  const attachments = adapter(f.frame, origin, f.shell.selection, text => notices.push(text));
  f.window.addEventListener("oc-box:selection-changed", attachments.invalidate);
  let release, reading;
  const stalled = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { reading = resolve; });
  class PausedFile extends File {
    async arrayBuffer() { reading(); await stalled; return super.arrayBuffer(); }
  }
  const bytes = Uint8Array.from([137, 80, 78, 71, 13, 10, 26, 10]);
  const adding = attachments.add([new PausedFile([bytes], "synthetic.png")]);
  await started;
  f.switchView("manager");
  assert.equal(attachments.available(), false);
  f.switchView("studio");
  assert.equal(attachments.available(), true);
  release();
  await adding;
  assert.equal(forwarded, 0);
  assert.match(notices.at(-1), /Nothing was forwarded/);
}

{
  const { root, get, switchView, resize } = boot();
  const left = get("workspace-divider-left");
  const right = get("workspace-divider-right");
  const widths = () => ["left", "right"].map(side => parseInt(root.style.get("--workspace-" + side)));
  assert.deepEqual(widths(), [248, 278]);
  assert(left.emit("pointerdown", { pointerId: 1, clientX: 248, button: 0, isPrimary: true }).defaultPrevented);
  left.emit("pointermove", { pointerId: 2, clientX: 800 });
  assert.deepEqual(widths(), [248, 278]);
  left.emit("pointermove", { pointerId: 1, clientX: 800 });
  assert.deepEqual(widths(), [552, 208]); // Preserve 420px chat + two 10px gutters.
  assert.equal(left.getAttribute("aria-valuenow"), "552");
  left.emit("pointerup", { pointerId: 1 });
  assert.equal(root.dataset.resizing, undefined);
  resize(1360, true);
  assert.deepEqual(widths(), [552, 278]); // Opposite pane's preference returns as space allows.
  switchView("dashboard");
  switchView("studio");
  assert.deepEqual(widths(), [552, 278]);
  assert(right.emit("keydown", { key: "ArrowLeft", shiftKey: true }).defaultPrevented);
  assert.deepEqual(widths(), [552, 342]);
  left.emit("dblclick");
  right.emit("keydown", { key: "Enter" });
  assert.deepEqual(widths(), [248, 278]);
  right.emit("pointerdown", { pointerId: 3, clientX: 1000, button: 0, isPrimary: true });
  resize(650, false);
  assert.equal(right.hasPointerCapture(3), false);
  assert.equal(root.dataset.resizing, undefined);
  resize(1360, true);
  switchView("dashboard");
  left.emit("keydown", { key: "End" });
  assert.equal(widths()[0], 930); // Dashboard has one gutter and no right pane.
  assert.equal(left.getAttribute("aria-valuemax"), "930");
}
console.log("PASS Studio: one authenticated shell, native frame and draft retained, searchable manager, selection invalidation and bounded Studio/Dashboard dividers.");
