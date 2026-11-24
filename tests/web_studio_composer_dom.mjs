import assert from "node:assert/strict";
import test from "node:test";
import { createStudioComposerView } from "../src/oc_box/web_studio_composer.mjs";
import { createStudioTranscriptView } from "../src/oc_box/web_studio_transcript.mjs";

class Element {
  constructor(document, tag) {
    this.ownerDocument = document;
    this.localName = tag;
    this.nodes = [];
    this.attrs = new Map();
    this.listeners = new Map();
    this.id = this.className = this.copy = this.value = "";
    this.parentNode = null;
  }
  hasChildNodes() { return this.nodes.length > 0 || !!this.copy; }
  append(...nodes) {
    for (const node of nodes) {
      if (node instanceof Element) {
        node.remove();
        node.parentNode = this;
      }
      this.nodes.push(node);
    }
  }
  remove() {
    if (!this.parentNode) return;
    this.parentNode.nodes.splice(this.parentNode.nodes.indexOf(this), 1);
    this.parentNode = null;
  }
  replaceChildren(...nodes) {
    for (const child of this.children) child.parentNode = null;
    this.nodes = [];
    this.copy = "";
    this.append(...nodes);
  }
  get children() { return this.nodes.filter(node => node instanceof Element); }
  get isConnected() { return this === this.ownerDocument.body || !!this.parentNode?.isConnected; }
  closest(tag) { return this.localName === tag ? this : this.parentNode?.closest(tag) ?? null; }
  setAttribute(name, value) { this.attrs.set(name, value); }
  set textContent(value) { this.replaceChildren(); this.copy = value; }
  get textContent() { return this.copy + this.nodes.map(node => node.textContent ?? node).join(""); }
  set innerHTML(_) { assert.fail("Studio content must not parse HTML"); }
  get tree() { return [this, ...this.children.flatMap(child => child.tree)]; }
  addEventListener(type, fn) {
    assert(!this.listeners.has(type), "fixture supports one listener per event");
    this.listeners.set(type, fn);
  }
  // Model delivery even on a disabled control to check the JS guards too.
  fire(type, isTrusted) {
    return this.listeners.get(type)?.({ type, isTrusted, currentTarget: this });
  }
}

function fixture(transport = async () => "queued") {
  const document = { createElement: tag => new Element(document, tag) };
  document.createElementNS = (_, tag) => document.createElement(tag);
  document.body = document.createElement("body");
  const center = document.createElement("main");
  const frame = document.createElement("iframe");
  const transcriptMount = document.createElement("div");
  const mount = document.createElement("div");
  frame.setAttribute("src", "/opaque/native/frame");
  center.id = "workspace-studio-center";
  transcriptMount.id = "workspace-studio-transcript";
  mount.id = "workspace-studio-composer";
  center.append(transcriptMount, mount);
  document.body.append(center, frame);
  const initial = { sessionID: "ses_example", generation: 1 };
  let selected = initial;
  const readSelection = () => typeof selected === "function" ? selected() : selected;
  const transcript = createStudioTranscriptView(transcriptMount, readSelection);
  const actions = [];
  const view = createStudioComposerView(mount, readSelection, action => {
    actions.push(action);
    return transport(action);
  });
  const editor = () => mount.tree.find(node => node.localName === "textarea");
  const send = mount.tree.find(node => node.localName === "button");
  const status = mount.tree.find(node => node.attrs.get("role") === "status");
  return {
    document, center, transcriptMount, transcript, mount, frame, view, send, status, actions,
    get editor() { return editor(); },
    select(value) { selected = value; },
    type(value, trusted = true) {
      const field = editor();
      field.value = value;
      field.fire("input", trusted);
    },
    click(trusted = true) { return send.fire("click", trusted); },
  };
}

function deferred() {
  let settle;
  const promise = new Promise(resolve => { settle = resolve; });
  return { promise, settle };
}

test("approved composer classes and only real controls, stable alongside the transcript", async () => {
  const f = fixture();
  assert(f.mount.hidden);
  f.view.refresh();
  assert(!f.mount.hidden);
  assert.deepEqual(f.mount.children.map(node => node.className),
    ["compose", "composition-note", "composition-note composition-status"]);
  const [compose, note] = f.mount.children;
  assert.deepEqual(compose.children.map(node => node.localName), ["textarea", "div"]);
  assert.equal(compose.children[1].className, "compose-foot");
  assert.equal(f.editor.placeholder, "Ask about the workspace…");
  assert.equal(f.editor.attrs.get("aria-describedby"), note.id);
  assert.equal(f.editor.attrs.get("autocomplete"), "off");
  assert.equal(f.editor.spellcheck, false);
  assert.equal(f.send.type, "button");
  assert.equal(f.send.className, "send");
  assert.equal(f.send.textContent.trim(), "Send");
  assert.equal(f.send.children[0].attrs.get("aria-hidden"), "true");
  assert.equal(f.send.children[0].children[0].attrs.get("d"), "M12 20V4m-6 6 6-6 6 6");
  assert(f.mount.tree.every(node => !["form", "a", "input", "select", "iframe"].includes(node.localName) &&
    !node.attrs.has("name") && !node.attrs.has("form")));
  const text = "  First line\n\tSecond line <img src=ignored>  ";
  f.type(text);
  const projection = {
    sessionID: "ses_example", title: "Native conversation", messages: [], hasEarlier: false,
  };
  assert(f.transcript.commit(f.transcript.begin(), projection));
  assert(f.transcript.begin()); // The read view can clear while its sibling keeps the draft.
  assert(f.transcript.commit(f.transcript.begin(), projection));
  f.view.refresh();
  assert.deepEqual(f.center.children, [f.transcriptMount, f.mount]);
  assert.equal(f.transcriptMount.textContent.includes(text), false);
  assert.equal(f.editor.value, text);
  assert.deepEqual(f.actions, []);
  await f.click();
  assert.equal(f.frame.attrs.get("src"), "/opaque/native/frame");
  assert.deepEqual(f.actions, [{ sessionID: "ses_example", generation: 1, text }]);
  assert(Object.isFrozen(f.actions[0]));
  assert.equal(f.editor.value, "");
  assert(f.send.disabled);
  assert.match(f.status.textContent, /acknowledged.*Execution is not confirmed/);
  await f.click();
  assert.equal(f.actions.length, 1);
});

test("input, Enter in the draft, synthetic clicks and stale/programmatic text cannot Send", async () => {
  const f = fixture();
  f.view.refresh();
  assert(f.send.disabled);
  f.type("a draft", false);
  await f.click();
  f.type("real draft");
  assert(!f.send.disabled);
  f.editor.fire("keydown", true);
  f.editor.fire("submit", true);
  await f.click(false);
  f.type("changed outside trusted input", false);
  await f.click();
  f.type("composing a draft");
  f.editor.fire("compositionstart", true);
  await f.click();
  f.editor.fire("compositionend", false);
  await f.click();
  assert.deepEqual(f.actions, []);
  f.editor.fire("compositionend", true);
  await f.click();
  assert.equal(f.actions[0].text, "composing a draft");

  f.type("do not reroute");
  f.select({ sessionID: "ses_other", generation: 2 });
  await f.click(); // Even if notification of the change has not reached refresh().
  assert.equal(f.actions.length, 1);
  assert(f.mount.hidden && f.send.disabled);
  assert.equal(f.editor.value, "");
  f.view.refresh();
  assert(!f.mount.hidden && f.send.disabled);
  assert.equal(f.editor.value, "");
});

test("UTF-8 bound, blank and control refusals never truncate and transmit a prefix", async () => {
  const f = fixture();
  f.view.refresh();
  for (const text of ["", " \t\n ", "\u0000", "valid\u0085no", "surrogate\ud800", "A".repeat(4097), "🦉".repeat(1025)]) {
    f.type(text);
    assert(f.send.disabled);
    await f.click();
  }
  assert.deepEqual(f.actions, []);
  assert.match(f.status.textContent, /4 KiB/);
  f.type("a draft before pasting");
  f.type("A".repeat(4097));
  assert.equal(f.editor.value, "a draft before pasting");
  assert(f.send.disabled);
  await f.click();
  assert.deepEqual(f.actions, []);
  const boundary = "🦉".repeat(1024);
  f.type(boundary);
  await f.click();
  assert.equal(f.actions[0].text, boundary);
});

test("a delayed input or composition event from a replaced field cannot become a new draft", async () => {
  const f = fixture();
  f.view.refresh();
  const former = f.editor;
  former.fire("compositionstart", true);
  f.type("old draft");
  f.select({ sessionID: "ses_other", generation: 2 });
  f.view.refresh();
  assert.notEqual(f.editor, former);
  assert.equal(former.value, "");
  former.value = "late edit from the departed selection";
  former.fire("input", true);
  former.fire("compositionend", true);
  assert.equal(f.editor.value, "");
  assert(f.send.disabled);
  await f.click();
  assert.deepEqual(f.actions, []);
  f.type("new draft");
  await f.click();
  assert.deepEqual(f.actions, [{ sessionID: "ses_other", generation: 2, text: "new draft" }]);
});

test("one action remains in flight, and an acknowledgment never erases a newer revision", async () => {
  const reply = deferred();
  const f = fixture(() => reply.promise);
  f.view.refresh();
  f.type("first draft");
  const work = f.click();
  assert(f.send.disabled);
  assert.match(f.status.textContent, /Sending to native OpenCode/);
  await f.click();
  f.type("editing while awaiting a result");
  await f.click();
  f.type("first draft"); // A real edit away and back is still a new revision.
  reply.settle("queued");
  await work;
  assert.equal(f.actions.length, 1);
  assert.equal(f.editor.value, "first draft");
  assert(!f.send.disabled);
  assert.match(f.status.textContent, /acknowledged/);
});

test("clear and away/back leave late receipts unable to affect a new draft", async () => {
  const reply = deferred();
  const f = fixture(() => reply.promise);
  f.view.refresh();
  f.type("old generation");
  const work = f.click();
  f.view.clear();
  assert(f.mount.hidden);
  assert.equal(f.editor.value, "");
  f.select({ sessionID: "ses_elsewhere", generation: 2 });
  f.view.refresh();
  f.type("other conversation");
  f.select({ sessionID: "ses_example", generation: 3 });
  f.view.refresh();
  assert.equal(f.editor.value, "");
  f.type("new generation");
  assert(f.send.disabled);
  await f.click();
  reply.settle("queued");
  await work;
  assert.deepEqual(f.actions, [{ sessionID: "ses_example", generation: 1, text: "old generation" }]);
  assert.equal(f.editor.value, "new generation");
  assert.equal(f.status.textContent, "");
  assert(!f.send.disabled);
});

test("a late result rechecks the live generation even without a shell notification", async () => {
  const reply = deferred();
  const f = fixture(() => reply.promise);
  const snapshot = { sessionID: "ses_example", generation: 1 };
  f.select(snapshot);
  f.view.refresh();
  f.type("bound to the old generation");
  const work = f.click();
  snapshot.generation = 2;
  reply.settle("unconfirmed");
  await work;
  assert.equal(f.actions[0].generation, 1);
  assert(f.mount.hidden && f.send.disabled);
  assert.equal(f.editor.value, "");
  assert.equal(f.status.textContent, "");
});

test("only verified refusal permits another attempt on the current draft; ambiguity is sticky", async () => {
  const refused = fixture(async () => "refused");
  refused.view.refresh();
  refused.type("try once");
  await refused.click();
  assert.match(refused.status.textContent, /Not sent/);
  assert.equal(refused.editor.value, "try once");
  assert(!refused.send.disabled);
  assert.equal(refused.actions.length, 1);

  const unexpected = [
    () => "unconfirmed",
    () => ({ status: "queued", secret: "do not show transport internals" }),
    () => { throw new Error("do not show transport internals"); },
    async () => { throw new Error("do not show transport internals"); },
    () => undefined,
  ];
  for (const transport of unexpected) {
    const f = fixture(transport);
    f.view.refresh();
    f.type("possibly queued");
    await f.click();
    assert.match(f.status.textContent, /unconfirmed.*already be queued.*native OpenCode/);
    assert(!f.mount.textContent.includes("do not show transport internals"));
    assert.equal(f.editor.value, "possibly queued");
    f.view.refresh();
    await f.click();
    f.type("edited draft");
    await f.click();
    assert.equal(f.editor.value, "edited draft");
    assert(f.send.disabled);
    assert.match(f.status.textContent, /unconfirmed/);
    assert.equal(f.actions.length, 1);
  }
});

test("bad snapshots, a detached mount or moving under a form cannot authorize draft transmission", async () => {
  const f = fixture();
  const valid = { sessionID: "ses_example", generation: 1 };
  for (const invalid of [
    null, { ...valid, sessionID: "ses_example\n" }, { ...valid, sessionID: "ses_../example" },
    { ...valid, generation: -1 }, { ...valid, generation: 2 ** 53 }, { ...valid, generation: "1" },
    () => { throw new Error("not a UI message"); },
  ]) {
    f.select(valid);
    f.view.refresh();
    f.type("owned before selection was invalidated");
    f.select(invalid);
    await f.click();
    f.view.refresh();
    assert(f.mount.hidden);
    assert.equal(f.editor.value, "");
  }
  f.select(valid);
  f.view.refresh();
  f.type("now detached");
  f.mount.remove();
  await f.click();
  assert(f.mount.hidden);
  const form = f.document.createElement("form");
  f.center.append(f.mount, form);
  f.view.refresh();
  f.type("now inside a form");
  form.append(f.mount);
  await f.click();
  assert(f.mount.hidden);
  assert.deepEqual(f.actions, []);

  const occupied = f.document.createElement("div");
  const inside = f.document.createElement("div");
  occupied.id = inside.id = f.mount.id;
  occupied.append(f.document.createElement("p"));
  form.append(inside);
  for (const bad of [null, f.frame, f.transcriptMount, f.mount, occupied, inside]) {
    assert.throws(() => createStudioComposerView(bad, () => valid, () => "queued"), TypeError);
  }
});
