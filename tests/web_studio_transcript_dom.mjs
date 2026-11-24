import assert from "node:assert/strict";
import test from "node:test";
import { createStudioTranscriptView } from "../src/oc_box/web_studio_transcript.mjs";

class Element {
  constructor(document, tag) {
    this.ownerDocument = document;
    this.localName = tag;
    this.children = [];
    this.attrs = new Map();
    this.id = this.className = this.copy = "";
  }
  hasChildNodes() { return this.children.length > 0; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(name, value) { this.attrs.set(name, value); }
  set textContent(value) { this.copy = value; this.replaceChildren(); }
  get textContent() { return this.copy + this.children.map(node => node.textContent).join(""); }
  set innerHTML(_) { assert.fail("transcript must not parse HTML"); }
  get tree() { return [this, ...this.children.flatMap(child => child.tree)]; }
}

function fixture() {
  const document = { createElement: tag => new Element(document, tag) };
  const shell = document.createElement("section");
  const frame = document.createElement("iframe");
  const hostControls = document.createElement("nav");
  const mount = document.createElement("div");
  frame.setAttribute("src", "/opaque/native/frame");
  hostControls.textContent = "Existing controls";
  mount.id = "workspace-studio-transcript";
  shell.append(hostControls, mount, frame);
  let selected = { sessionID: "ses_example", generation: 1 };
  const view = createStudioTranscriptView(mount, () => selected);
  return {
    document, shell, frame, hostControls, mount, view,
    select(next) { selected = next; },
  };
}

const user = {
  id: "msg_example_prompt", role: "user", truncated: false,
  text: "First line\n\tSecond line <img src=http://example.invalid/>",
};
const answer = {
  id: "msg_example_answer", role: "assistant", truncated: true,
  text: "<script>throw 'not executed'</script>\nhttps://example.invalid/ # markdown is literal",
};
function projection(sessionID = "ses_example", messages = [user, answer]) {
  return { sessionID, title: "<b>Example title</b>", hasEarlier: true, messages };
}
const named = (mount, cls) => mount.tree.filter(node => node.className.split(" ").includes(cls));
const find = (mount, tag) => mount.tree.filter(node => node.localName === tag);

test("approved roles, badges and literal ordered messages are isolated from the shell", () => {
  const { shell, frame, hostControls, mount, view } = fixture();
  const ticket = view.begin();
  assert(view.commit(ticket, projection()));
  const [heading] = find(mount, "h2");
  assert.equal(heading.textContent, "<b>Example title</b>");
  assert.equal(heading.attrs.get("dir"), "auto");
  assert.equal(find(mount, "section")[0].attrs.get("aria-labelledby"), heading.id);
  assert.deepEqual(named(mount, "message").map(node => node.className), ["message you", "message"]);
  assert.deepEqual(find(mount, "strong").map(node => node.textContent), ["You", "OpenCode"]);
  assert.deepEqual(named(mount, "avatar").map(node => [node.textContent, node.attrs.get("aria-hidden")]),
    [["Y", "true"], ["O", "true"]]);
  assert.deepEqual(named(mount, "message").map(node => find(node, "p")[0].textContent), [user.text, answer.text]);
  assert.equal(named(mount, "transcript")[0].localName, "ol");
  assert.equal(named(mount, "transcript-earlier").length, 1);
  assert.equal(named(mount, "message")[0].tree.includes(named(mount, "transcript-clipped")[0]), false);
  assert.equal(named(mount, "transcript-clipped")[0].textContent, "Message clipped.");
  assert(mount.tree.every(node => !["img", "script", "a", "input", "textarea", "form", "button", "iframe"].includes(node.localName)));
  assert.deepEqual(shell.children, [hostControls, mount, frame]);
  assert.equal(frame.attrs.get("src"), "/opaque/native/frame");
  assert.equal(hostControls.textContent, "Existing controls");
});

test("notices distinguish a bounded empty window from malformed or unavailable input", () => {
  const { mount, view } = fixture();
  assert(view.commit(view.begin(), { ...projection("ses_example", []), hasEarlier: false }));
  assert.match(mount.textContent, /No text messages are available/);
  assert.equal(named(mount, "transcript-earlier").length, 0);
  assert.equal(named(mount, "message").length, 0);
  assert(!view.commit(view.begin(), null));
  assert.match(mount.textContent, /Read-only transcript unavailable/);
  assert(!mount.textContent.includes("<b>Example title</b>"));
  assert(!mount.textContent.includes("No text messages"));
});

test("strict projection shape, UTF-8 limits, roles, IDs, newlines and absent fields", () => {
  let getterRead = false;
  const trap = Object.defineProperty({ ...user }, "text", {
    enumerable: true, get() { getterRead = true; return user.text; },
  });
  const oversizedWindow = Array.from({ length: 72 }, (_, i) => ({
    ...user, id: "msg_max_" + i, text: '"'.repeat(4096),
  })); // Re-encoded JSON alone exceeds the 512 KiB contract.
  const extraArrayField = Object.assign([user], { ignore: true });
  const cases = [
    projection("ses_other"), { ...projection(), credentials: "do not show me" },
    { ...projection(), title: "🦉".repeat(65) }, { ...projection(), title: "title\n" },
    { ...projection(), hasEarlier: "false" }, { ...projection(), messages: extraArrayField },
    projection("ses_example", [user, user]), projection("ses_example", Array(1)),
    projection("ses_example", [user, { ...answer, role: "tool" }]),
    projection("ses_example", [{ ...user, id: "msg_example\n" }]),
    projection("ses_example", [{ ...user, id: "msg_../example" }]),
    projection("ses_example", [{ ...user, text: "\u202eUnsafe" }]),
    projection("ses_example", [{ ...user, text: "\ud800" }]),
    projection("ses_example", [{ ...user, text: "é".repeat(2049) }]),
    projection("ses_example", [{ ...user, truncated: 1 }]),
    projection("ses_example", [trap]), projection("ses_example", oversizedWindow),
    projection("ses_example", Array.from({ length: 73 }, (_, i) => ({ ...user, id: "msg_over_" + i }))),
  ];
  const { mount, view } = fixture();
  for (const bad of cases) {
    assert(!view.commit(view.begin(), bad));
    assert.equal(named(mount, "message").length, 0);
    assert.match(mount.textContent, /Read-only transcript unavailable/);
    assert(!mount.textContent.includes(user.text));
  }
  assert.equal(getterRead, false);
  const atBound = { ...projection("ses_example", [{ ...user, text: "🦉".repeat(1024) }]),
    title: "🦉".repeat(64) };
  assert(view.commit(view.begin(), atBound));
});

test("late results cannot overwrite another generation, selection or same-selection read", async () => {
  const { select, mount, view } = fixture();
  const departed = view.begin();
  select({ sessionID: "ses_elsewhere", generation: 2 });
  view.clear();
  select({ sessionID: "ses_example", generation: 3 });
  const returned = view.begin();
  const late = Promise.resolve(projection()).then(value => view.commit(departed, value));
  const newest = view.begin();
  assert(view.commit(newest, { ...projection(), title: "Verified current title" }));
  assert.equal(await late, false);
  assert(!view.commit(returned, projection()));
  assert(!view.commit({ ...newest }, projection()));
  assert(!view.commit(newest, projection())); // Single use, even on identical generation.
  assert.match(mount.textContent, /Verified current title/);

  const sameIDOldGeneration = view.begin();
  select({ sessionID: "ses_example", generation: 4 });
  assert(!view.commit(sameIDOldGeneration, projection()));
  assert(!mount.hasChildNodes());

  const wrongIDSameGeneration = view.begin();
  select({ sessionID: "ses_renamed", generation: 4 });
  assert(!view.commit(wrongIDSameGeneration, projection()));
  assert(!mount.hasChildNodes());

  select(null);
  assert.equal(view.begin(), null);
  assert(!view.commit(null, projection()));
  select({ sessionID: "ses_renamed", generation: -1 });
  assert.equal(view.begin(), null);
});

test("refresh preserves a verified projection until the current replacement is valid", () => {
  const { view, mount } = fixture();
  assert.equal(view.beginRefresh(), null);
  assert(view.commit(view.begin(), projection()));
  const original = mount.children[0];
  for (const bad of [null, projection("ses_other"), projection("ses_example", [user, user])]) {
    const refresh = view.beginRefresh();
    assert.equal(mount.children[0], original);
    assert(!view.commit(refresh, bad));
    assert.equal(mount.children[0], original);
    assert(!view.commit(refresh, projection())); // A failed ticket is also spent.
  }
  const superseded = view.beginRefresh();
  const refresh = view.beginRefresh();
  assert(!view.commit(superseded, projection()));
  assert.equal(mount.children[0], original);
  assert(view.commit(refresh, { ...projection(), title: "Current refreshed title" }));
  assert.notEqual(mount.children[0], original);
  assert.match(mount.textContent, /Current refreshed title/);
});

test("preserving refresh never keeps old content on observed invalidation or overwrites a newer scope", () => {
  const { view, mount, select } = fixture();
  assert(view.commit(view.begin(), projection()));
  const stale = view.beginRefresh();
  select({ sessionID: "ses_example", generation: 2 });
  assert(!view.commit(stale, projection()));
  assert(!mount.hasChildNodes());

  assert(view.commit(view.begin(), projection()));
  const departed = view.beginRefresh();
  select({ sessionID: "ses_other", generation: 3 });
  assert.equal(view.beginRefresh(), null);
  assert(!mount.hasChildNodes());
  select({ sessionID: "ses_example", generation: 4 });
  assert(view.commit(view.begin(), { ...projection(), title: "Same ID, new generation" }));
  assert(!view.commit(departed, null));
  assert.match(mount.textContent, /Same ID, new generation/);
});

test("selection invalidated while the detached nodes are being prepared is rechecked", () => {
  const { select, document, mount, view } = fixture();
  const ticket = view.begin();
  const create = document.createElement;
  document.createElement = tag => {
    select({ sessionID: "ses_example", generation: 9 });
    return create(tag);
  };
  assert(!view.commit(ticket, projection()));
  assert(!mount.hasChildNodes());
});

test("cannot mount over the frame, existing controls or a previously bound slot", () => {
  const { document, mount, shell, frame, hostControls } = fixture();
  const filled = document.createElement("div");
  filled.id = mount.id;
  filled.append(document.createElement("nav"));
  for (const bad of [shell, frame, hostControls, mount, filled, null]) {
    assert.throws(() => createStudioTranscriptView(bad, () => null), TypeError);
  }
});
