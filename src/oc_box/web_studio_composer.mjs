// Source only. The first-party shell owns selection, admission and transport.
const mountID = "workspace-studio-composer";
const noteID = mountID + "-note";
const mounted = new WeakSet();
const sessionIDPattern = /^ses[A-Za-z0-9_-]{1,125}$/u;
// Keep in step with the text limit in web_studio_send.py. The native adapter
// still validates the text and encoded request independently.
const maxText = 4096;
const controls = /[\x00-\x08\x0b-\x1f\x7f-\x9f\p{Cs}]/u;
const encoder = new TextEncoder();
const unconfirmed = "Send unconfirmed. The message may already be queued. Check this conversation in native OpenCode before trying again.";

function sameSelection(a, b) {
  return a !== null && b !== null &&
    a.sessionID === b.sessionID && a.generation === b.generation;
}

function sendIcon(document) {
  const namespace = "http://www.w3.org/2000/svg";
  const icon = document.createElementNS(namespace, "svg");
  icon.setAttribute("class", "icon");
  icon.setAttribute("viewBox", "0 0 24 24");
  icon.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(namespace, "path");
  path.setAttribute("d", "M12 20V4m-6 6 6-6 6 6");
  icon.append(path);
  return icon;
}

/**
 * Mount once in a stable, empty, first-party div outside the transcript mount
 * and any form. Use the same trusted readSelection() as the transcript, or a
 * stricter one returning null when Send is unavailable. It supplies
 * {sessionID, generation}; advance the generation on every observed
 * invalidation, including an away-and-back transition to the same ID.
 *
 * Call refresh() on entry and after trusted selection changes. Call clear()
 * immediately when leaving Studio or invalidating selection, owner, document
 * or admission. A new generation always starts with an empty in-page draft.
 * The shell must independently fence and cancel outstanding admission.
 *
 * send({sessionID, generation, text}) is the injected, one-action async
 * adapter, called once directly from a trusted Send click. It must recheck
 * admission and the same exact selection through delivery. Resolve to "queued"
 * only for a verified native acknowledgement, "refused" only if no native
 * POST was attempted, or "unconfirmed". Throws/rejections and other values
 * are unconfirmed, not permission to retry. The view does not authenticate
 * actions or implement any endpoint, permit or native work.
 */
export function createStudioComposerView(mount, readSelection, send) {
  if (mount?.localName !== "div" || mount.id !== mountID ||
      mount.hasChildNodes() || mounted.has(mount) || mount.closest("form") ||
      typeof readSelection !== "function" || typeof send !== "function") {
    throw new TypeError("Studio composer requires its own empty mount and scoped callbacks.");
  }
  mounted.add(mount);
  const document = mount.ownerDocument;
  const compose = document.createElement("div");
  compose.className = "compose";
  const foot = document.createElement("div");
  foot.className = "compose-foot";
  const button = document.createElement("button");
  button.className = "send";
  button.type = "button";
  button.append(sendIcon(document), " Send");
  foot.append(button);
  const note = document.createElement("p");
  note.id = noteID;
  note.className = "composition-note";
  note.textContent = "Drafts stay in this page and are discarded when the selection changes. Send queues text in native OpenCode.";
  const status = document.createElement("p");
  status.className = "composition-note composition-status";
  status.setAttribute("role", "status");
  status.setAttribute("aria-atomic", "true");

  let editor = null;
  let draft = null;
  let pending = null;
  let composing = false;

  function current() {
    if (!mount.isConnected || mount.ownerDocument !== document || mount.closest("form")) return null;
    try {
      const { sessionID, generation } = readSelection() ?? {};
      if (typeof sessionID === "string" && sessionIDPattern.test(sessionID) &&
          Number.isSafeInteger(generation) && generation >= 0) return { sessionID, generation };
    } catch { /* An unverifiable selection cannot own a draft. */ }
    return null;
  }

  function render() {
    mount.hidden = draft === null;
    editor.disabled = draft === null;
    button.disabled = !draft?.valid || draft.blocked || pending !== null || composing ||
      editor.value !== draft.text;
    const copy = draft === null ? "" : pending === null ? draft.notice :
      pending === draft ? "Sending to native OpenCode…" : "Waiting for a previous Send to settle…";
    if (status.textContent !== copy) status.textContent = copy;
  }

  function clear() {
    draft = null;
    composing = false;
    // Replace the field across generations so a delayed edit or IME
    // completion from the old field can never populate a new draft.
    if (editor !== null) editor.value = "";
    editor = document.createElement("textarea");
    editor.spellcheck = false;
    editor.setAttribute("autocomplete", "off");
    editor.setAttribute("autocorrect", "off");
    editor.setAttribute("dir", "auto");
    editor.setAttribute("aria-label", "Message");
    editor.setAttribute("aria-describedby", noteID);
    editor.placeholder = "Ask about the workspace…";
    editor.addEventListener("input", captureInput);
    editor.addEventListener("compositionstart", beginComposition);
    editor.addEventListener("compositionend", endComposition);
    compose.replaceChildren(editor, foot);
    render();
  }

  function refresh() {
    const selection = current();
    if (draft === null && selection === null) return;
    if (!sameSelection(draft?.selection ?? null, selection)) {
      clear();
      if (selection !== null) {
        draft = { selection, text: "", revision: null, valid: false, blocked: false, notice: "" };
        render();
      }
    }
  }

  function stillCurrent(owner) {
    const selection = current();
    if (draft !== owner) return false;
    if (sameSelection(owner.selection, selection)) return true;
    clear();
    return false;
  }

  function editorOwner(event) {
    const owner = draft;
    return event.isTrusted === true && event.currentTarget === editor &&
      owner !== null && stillCurrent(owner) ? owner : null;
  }

  function captureInput(event) {
    const owner = editorOwner(event);
    if (owner === null) return;
    const text = editor.value;
    const tooLong = text.length > maxText || encoder.encode(text).byteLength > maxText;
    if (text !== owner.text) owner.revision = {};
    if (text.length > maxText) {
      // HTML maxlength can silently clip a paste. Reject instead of
      // retaining oversized text or making a truncated prefix sendable.
      editor.value = owner.text;
    } else {
      owner.text = text;
    }
    const invalid = controls.test(owner.text);
    owner.valid = !tooLong && !invalid && !!owner.text.trim();
    if (!owner.blocked) {
      owner.notice = tooLong ? "Message exceeds the 4 KiB text limit." :
        invalid ? "Message contains unsupported control characters." : "";
    }
    render();
  }

  function beginComposition(event) {
    if (editorOwner(event) === null) return;
    composing = true;
    render();
  }

  function endComposition(event) {
    if (editorOwner(event) === null) return;
    composing = false;
    render();
  }

  button.addEventListener("click", async event => {
    const owner = draft;
    if (event.isTrusted !== true || owner === null || pending !== null || composing ||
        owner.blocked || !owner.valid || !stillCurrent(owner) || editor.value !== owner.text) return;
    const { revision, text } = owner;
    const action = Object.freeze({ ...owner.selection, text });
    pending = owner;
    render();
    let result;
    try { result = await send(action); }
    catch { /* A callback failure cannot prove that a write did not happen. */ }
    pending = null;
    const visible = draft;
    if (visible !== null && !stillCurrent(visible)) return;
    if (visible === owner) {
      if (result === "queued") {
        // Even editing away and back to the same text creates a newer draft.
        if (owner.revision === revision && editor.value === text) {
          editor.value = owner.text = "";
          owner.valid = false;
        }
        owner.notice = "OpenCode acknowledged the queued message. Execution is not confirmed.";
      } else if (result === "refused") {
        owner.notice = "Not sent. Native OpenCode was not asked to queue this message.";
      } else {
        owner.blocked = true;
        owner.notice = unconfirmed;
      }
    }
    render();
  });

  clear();
  mount.append(compose, note, status);
  return Object.freeze({ refresh, clear });
}
