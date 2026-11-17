// Synthetic browser event contract, not live Spectacle/browser acceptance.
import assert from "node:assert/strict";
import { File } from "node:buffer";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const api = require("../src/oc_box/web_attachments.js");
const png = Uint8Array.from([137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 0]);
const image = () => new File([png], "screenshot.png", { type: "application/octet-stream" });
const origin = "http://127.0.0.1:5555";
const route = "/server/local/session/ses_synthetic";

class Transfer {
  constructor() {
    this.files = [];
    this.strings = {};
    this.types = [];
    this.items = { add: (file) => {
      this.files.push(file);
      if (!this.types.includes("Files")) this.types.push("Files");
    } };
  }
  getData(type) { return this.strings[type] || ""; }
}
class Drop {
  constructor(type, options) { Object.assign(this, { type, isTrusted: false }, options); }
  preventDefault() { this.defaultPrevented = true; }
  stopImmediatePropagation() { this.stopped = true; }
}

function fixture() {
  const listeners = [];
  const received = [];
  const notices = [];
  const doc = {
    addEventListener(type, handler, capture = false) { listeners.push({ type, handler, capture }); },
    dispatchEvent(event) {
      event.target ??= doc;
      for (const listener of [...listeners].sort((a, b) => Number(b.capture) - Number(a.capture))) {
        if (event.stopped) break;
        if (listener.type === event.type) listener.handler(event);
      }
      return !event.defaultPrevented;
    },
  };
  const realm = { File, DataTransfer: Transfer, DragEvent: Drop, document: doc, location: { href: origin + route } };
  const frame = { contentWindow: realm };
  let done;
  const settled = new Promise((resolve) => { done = resolve; });
  const current = api.adapter(frame, origin, route, (text) => { notices.push(text); done(); });
  current.install();
  // Faithful bounded representation of the pinned native precedence contract:
  // text/plain file: first, then File bytes. This is NOT a native/browser pass.
  doc.addEventListener("drop", (event) => {
    const text = event.dataTransfer.getData("text/plain");
    if (text.startsWith("file:")) received.push({ mention: text });
    else received.push({ files: [...event.dataTransfer.files] });
  });
  return { doc, frame, realm, current, received, notices, settled };
}

function transfer(files, text) {
  const result = new Transfer();
  for (const file of files) result.items.add(file);
  if (text) result.strings["text/plain"] = text;
  return result;
}

// File-only forwarding removes competing URL/plain-text flavors, not just paths
// from a particular desktop. There is no model request or second approval.
{
  const f = fixture();
  const data = transfer([image()], "file:///host/private/screenshot.png");
  data.strings["text/uri-list"] = "file:///host/private/screenshot.png";
  f.doc.dispatchEvent(new Drop("drop", { dataTransfer: data, isTrusted: true }));
  await f.settled;
  assert.equal(f.received.length, 1);
  assert.equal(f.received[0].mention, undefined);
  assert.equal(f.received[0].files[0].type, "image/png");
  assert.deepEqual(new Uint8Array(await f.received[0].files[0].arrayBuffer()), png);
  assert.match(f.notices[0], /not submitted a prompt/);
}

// Native non-file drags keep native defaults, including workspace mentions.
{
  const f = fixture();
  f.doc.dispatchEvent(new Drop("drop", { dataTransfer: transfer([], "file:src/main.py"), isTrusted: true }));
  assert.deepEqual(f.received, [{ mention: "file:src/main.py" }]);
  assert.equal(f.notices.length, 0);
}

// A trusted drop forwards to exactly its original event target, not a selected
// widget or a document fallback. A detached target is refused, never retargeted.
for (const connected of [true, false]) {
  const f = fixture();
  let dispatches = 0;
  const target = {
    ownerDocument: f.doc, isConnected: connected,
    dispatchEvent(event) {
      dispatches++;
      event.target = target;
      return f.doc.dispatchEvent(event);
    },
  };
  f.doc.dispatchEvent(new Drop("drop", {
    dataTransfer: transfer([image()], "file:/host/screenshot.png"),
    isTrusted: true, target,
  }));
  await f.settled;
  assert.equal(dispatches, connected ? 1 : 0);
  assert.equal(f.received.length, connected ? 1 : 0);
  if (!connected) assert.match(f.notices[0], /original drop target/);
}

// The wrapper's own drop target accepts no URL/path-only payload or arbitrary type.
for (const files of [
  [],
  [new File(["/host/private.png"], "path.txt")],
  [new File(["PK\x03\x04word/document.xml"], "report.docx")],
  [image(), new File(["not an image"], "spoof.png", { type: "image/png" })],
]) {
  const f = fixture();
  await f.current.add(files);
  assert.equal(f.received.length, 0);
  assert.match(f.notices[0], /Choose|recognizable/);
}

// Only known native image/PDF signatures are normalized; naming and reported MIME
// are not authority. A magic check is not a full decoder or malware scanner.
for (const [bytes, expected] of [
  [png, "image/png"],
  [Uint8Array.of(255, 216, 255), "image/jpeg"],
  [Buffer.from("GIF89a"), "image/gif"],
  [Buffer.from("RIFF1234WEBP"), "image/webp"],
  [Buffer.from("%PDF-1.7\n"), "application/pdf"],
]) {
  const [file] = await api.snapshot([new File([bytes], "shot.bin")], { File });
  assert.equal(file.type, expected);
}

// Byte/count limits are checked before reading any file or sending a partial batch.
let reads = 0;
const large = (size) => ({ size, slice() { reads++; throw new Error("must not read"); } });
for (const files of [
  [large(0)], [large(api.MAX_FILE + 1)],
  [large(api.MAX_FILE), large(api.MAX_FILE), large(1)],
  Array.from({ length: 9 }, () => large(1)),
]) {
  await assert.rejects(api.snapshot(files, { File }));
}
assert.equal(reads, 0);
assert.equal(api.MAX_FILE, 20971520);
assert.equal(api.MAX_TOTAL, 41943040);

// Browser File snapshots must survive the source temporary file disappearing.
{
  const f = fixture();
  const source = image();
  await f.current.add([source]);
  assert.notEqual(f.received[0].files[0], source);
  assert.equal(f.received[0].files[0].type, "image/png");
}

// Route changes while reading cannot attach bytes to a different session.
{
  const f = fixture();
  const delayed = image();
  const original = delayed.arrayBuffer.bind(delayed);
  delayed.arrayBuffer = async () => {
    f.realm.location.href = origin + "/server/local/session/ses_other";
    return original();
  };
  await f.current.add([delayed]);
  assert.equal(f.received.length, 0);
  assert.match(f.notices[0], /session changed/);
}

// No foreign origin/server and no unknown active page receives a forwarded drop.
for (const href of [
  "https://outside.invalid/server/local/session/ses_synthetic",
  origin + "/server/other/session/ses_synthetic",
  origin + "/server/local/session/ses_wrong_target",
  origin + "/settings",
]) {
  const f = fixture();
  f.realm.location.href = href;
  await f.current.add([image()]);
  assert.equal(f.received.length, 0);
}

// Untrusted injected file events are not upgraded into a user file selection.
{
  const f = fixture();
  f.doc.dispatchEvent(new Drop("drop", { dataTransfer: transfer([image()]) }));
  assert.equal(f.received.length, 0);
  assert.match(f.notices[0], /actual browser file drop/);
}

console.log("PASS synthetic browser attachment forwarding and refusal matrix");
