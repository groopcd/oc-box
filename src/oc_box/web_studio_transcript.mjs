// Source only. The caller owns authorization, selection and the native frame.
const mountID = "workspace-studio-transcript";
const titleID = mountID + "-title";
const mounted = new WeakSet();
const sessionIDPattern = /^ses[A-Za-z0-9_-]{1,125}$/u;
const messageIDPattern = /^msg_[A-Za-z0-9_-]{1,124}$/u;
// This is the display-only contract of web_studio_conversation.py, not a
// parser for native API responses. Keep the UTF-8 limits in step with it.
const maxMessages = 3 * 24;
const maxTitle = 256;
const maxText = 4096;
const maxOutput = 512 * 1024;
const controls = /[\p{Cc}\p{Cf}\p{Cs}\p{Zl}\p{Zp}]/u;
const encoder = new TextEncoder();

function dataValue(record, key) {
  const field = Object.getOwnPropertyDescriptor(record, key);
  if (!field?.enumerable || !Object.hasOwn(field, "value")) throw new TypeError();
  return field.value;
}

function fields(record, keys) {
  if (!record || typeof record !== "object") throw new TypeError();
  const proto = Object.getPrototypeOf(record);
  if ((proto !== Object.prototype && proto !== null) ||
      Reflect.ownKeys(record).length !== keys.length) throw new TypeError();
  return Object.fromEntries(keys.map(key => [key, dataValue(record, key)]));
}

function text(value, limit, multiline = false) {
  if (typeof value !== "string" || value.length > limit || !value.trim() ||
      (!multiline && value !== value.trim()) ||
      controls.test(multiline ? value.replace(/[\n\t]/gu, "") : value) ||
      encoder.encode(value).byteLength > limit) throw new TypeError();
}

function checkedProjection(value, expectedID) {
  const checked = fields(value, ["sessionID", "title", "hasEarlier", "messages"]);
  const { sessionID, title, hasEarlier, messages } = checked;
  if (sessionID !== expectedID || typeof hasEarlier !== "boolean" ||
      !Array.isArray(messages) || Object.getPrototypeOf(messages) !== Array.prototype) {
    throw new TypeError();
  }
  const { value: length } = Object.getOwnPropertyDescriptor(messages, "length");
  if (!Number.isInteger(length) || length < 0 || length > maxMessages ||
      Reflect.ownKeys(messages).length !== length + 1) throw new TypeError();
  text(title, maxTitle);
  const seen = new Set();
  checked.messages = [];
  for (let i = 0; i < length; i++) {
    const row = fields(dataValue(messages, String(i)), ["id", "role", "text", "truncated"]);
    if (typeof row.id !== "string" || !messageIDPattern.test(row.id) || seen.has(row.id) ||
        (row.role !== "user" && row.role !== "assistant") || typeof row.truncated !== "boolean") {
      throw new TypeError();
    }
    text(row.text, maxText, true);
    seen.add(row.id);
    checked.messages.push(row);
  }
  if (encoder.encode(JSON.stringify(checked)).byteLength > maxOutput) throw new TypeError();
  return checked;
}

function element(document, tag, className = "", copy = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (copy) node.textContent = copy;
  return node;
}

function message(document, row) {
  const name = row.role === "user" ? "You" : "OpenCode";
  const entry = element(document, "article", row.role === "user" ? "message you" : "message");
  const by = element(document, "div", "by");
  const badge = element(document, "span", "avatar", name[0]);
  badge.setAttribute("aria-hidden", "true");
  by.append(badge, element(document, "strong", "", name));
  const body = element(document, "p", "", row.text);
  body.setAttribute("dir", "auto");
  entry.append(by, body);
  if (row.truncated) {
    entry.append(element(document, "small", "transcript-notice transcript-clipped", "Message clipped."));
  }
  const item = element(document, "li");
  item.append(entry);
  return item;
}

function transcript(document, projection) {
  const section = element(document, "section");
  section.setAttribute("aria-labelledby", titleID);
  const header = element(document, "header", "chat-head");
  const heading = element(document, "div");
  const title = element(document, "h2", "", projection?.title ?? "Conversation unavailable");
  title.id = titleID;
  title.setAttribute("dir", "auto");
  heading.append(element(document, "p", "eyebrow", "OpenCode / Read-only transcript"), title);
  header.append(heading);
  const body = element(document, "div", "chat-body");
  section.append(header, body);
  if (projection === null) {
    body.append(element(document, "p", "transcript-notice", "Read-only transcript unavailable."));
    return section;
  }
  if (projection.hasEarlier) {
    body.append(element(document, "p", "transcript-notice transcript-earlier",
      "Earlier messages may not be shown in this read-only window."));
  }
  if (projection.messages.length === 0) {
    body.append(element(document, "p", "transcript-notice",
      "No text messages are available in this read-only view."));
    return section;
  }
  const list = element(document, "ol", "transcript");
  list.setAttribute("role", "list");
  list.setAttribute("aria-label", "Conversation messages");
  for (const row of projection.messages) list.append(message(document, row));
  body.append(list);
  return section;
}

/**
 * Mount once in a dedicated, initially empty first-party div. readSelection()
 * synchronously supplies {sessionID, generation} or null from the trusted
 * shell. The generation changes on every observed selection invalidation,
 * including leaving and returning to the same conversation.
 */
export function createStudioTranscriptView(mount, readSelection) {
  if (mount?.localName !== "div" || mount.id !== mountID ||
      mount.hasChildNodes() || mounted.has(mount) || typeof readSelection !== "function") {
    throw new TypeError("Studio transcript requires its own empty mount.");
  }
  mounted.add(mount);
  const document = mount.ownerDocument;
  let pending = null;

  function current() {
    try {
      const { sessionID, generation } = readSelection() ?? {};
      if (typeof sessionID === "string" && sessionIDPattern.test(sessionID) &&
          Number.isSafeInteger(generation) && generation >= 0) return { sessionID, generation };
    } catch { /* An unverifiable selection is not a transcript target. */ }
    return null;
  }

  function clear() {
    pending = null;
    mount.replaceChildren();
  }

  function begin() {
    clear();
    const selection = current();
    if (selection === null) return null;
    pending = Object.freeze(selection);
    return pending;
  }

  function stillCurrent(ticket) {
    const live = current();
    return pending === ticket && live?.sessionID === ticket.sessionID &&
      live.generation === ticket.generation;
  }

  function commit(ticket, value) {
    // Object identity also separates two reads of an unchanged selection.
    if (ticket === null || pending !== ticket) return false;
    if (!stillCurrent(ticket)) {
      if (pending === ticket) clear();
      return false;
    }
    let checked = null;
    try { checked = checkedProjection(value, ticket.sessionID); }
    catch { /* Never display a raw failure or a partial projection. */ }
    const content = transcript(document, checked);
    // Validate the same generation again, before the one DOM insertion.
    if (!stillCurrent(ticket)) {
      if (pending === ticket) clear();
      return false;
    }
    pending = null;
    mount.replaceChildren(content);
    return checked !== null;
  }

  return Object.freeze({ begin, commit, clear });
}
