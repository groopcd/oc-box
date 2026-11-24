import { createStudioComposerView } from "./web_studio_composer.mjs";
import { createStudioTranscriptView } from "./web_studio_transcript.mjs";

const mounted = new WeakSet();
const sessionIDPattern = /^ses[A-Za-z0-9_-]{1,125}$/u;
const activeAttribute = "data-first-party-studio";

function sameSelection(a, b) {
  return a !== null && b !== null && a.sessionID === b.sessionID &&
    a.generation === b.generation && a.writable === b.writable;
}

/**
 * Trusted bootstrap only, after the native shell and the Studio CSS are ready.
 * The controller synchronously supplies {sessionID, generation, writable} or
 * null, reads a bounded transcript for {sessionID, generation}, and may send
 * {sessionID, generation, text}. A writable selection requires an admitted
 * send function. The existing native selection is a cross-check, not authority.
 *
 * Caller-owned: invalidation on observed route/document, visibility, owner or
 * admission changes; advance the generation even on away/back. Call invalidate()
 * and cancel the old scope before another present(). No shell event authorizes
 * either operation. The controller owns deadlines, revocation and cleanup.
 */
export function createStudioShellView(root, controller) {
  if (root?.id !== "web-shell" || mounted.has(root) ||
      typeof controller?.readSelection !== "function" ||
      typeof controller.readTranscript !== "function" ||
      (controller.send !== undefined && typeof controller.send !== "function")) return null;
  const document = root.ownerDocument;
  const window = document.defaultView;
  const shell = window?.ocBoxWebShell;
  const chat = root.querySelector(":scope > #workspace-content > #workspace-chat");
  const frame = chat?.querySelector(":scope > #native");
  const center = chat?.querySelector(":scope > #workspace-studio-center");
  const transcriptSlot = center?.querySelector(":scope > #workspace-studio-transcript");
  const composerSlot = center?.querySelector(":scope > #workspace-studio-composer");
  if (document.getElementById("web-shell") !== root || !root.isConnected ||
      !frame || shell?.version !== 1 || shell.frame !== frame ||
      typeof shell.selection !== "function" || !center?.hidden || !transcriptSlot || !composerSlot ||
      !composerSlot.hidden || transcriptSlot.hasChildNodes() || composerSlot.hasChildNodes() ||
      composerSlot.closest("form") || chat.hasAttribute(activeAttribute) ||
      window.getComputedStyle(center).getPropertyValue("--workspace-studio-style").trim() !== "ready") {
    return null;
  }
  const readSelection = controller.readSelection.bind(controller);
  const readTranscript = controller.readTranscript.bind(controller);
  const send = controller.send?.bind(controller);
  const nativeLabel = chat.getAttribute("aria-label");
  let binding = null;
  let visible = false;
  let reading = false;
  let highestGeneration = -1;

  function current() {
    if (!root.isConnected || window.ocBoxWebShell !== shell || !center.isConnected ||
        document.hidden || chat.hidden || frame.hidden) return null;
    try {
      const { sessionID, generation, writable } = readSelection() ?? {};
      if (typeof sessionID === "string" && sessionIDPattern.test(sessionID) &&
          Number.isSafeInteger(generation) && generation >= 0 &&
          shell.selection()?.sessionID === sessionID) {
        return { sessionID, generation, writable: !!send && writable === true };
      }
    } catch { /* No controller or native detail enters the view. */ }
    return null;
  }

  function selectedBinding() {
    const expected = binding;
    if (expected === null || !sameSelection(expected, current()) || binding !== expected) return null;
    return expected;
  }

  function composerSelection() {
    const expected = visible && selectedBinding();
    return expected?.writable ? expected : null;
  }

  const transcript = createStudioTranscriptView(transcriptSlot, selectedBinding);
  const composer = send ? createStudioComposerView(composerSlot, composerSelection, action => {
    const expected = composerSelection();
    // Narrow layout drawers make the chat inert, not a different selection.
    if (chat.inert || expected === null || expected.sessionID !== action.sessionID ||
        expected.generation !== action.generation) return "refused";
    return send(action);
  }) : null;
  mounted.add(root);

  function invalidate() {
    if (binding === null) return;
    binding = null; // Revoke the scope before clearing any DOM.
    visible = false;
    center.hidden = true;
    transcript.clear();
    composer?.clear();
    chat.removeAttribute(activeAttribute);
    if (nativeLabel === null) chat.removeAttribute("aria-label");
    else chat.setAttribute("aria-label", nativeLabel);
  }

  async function present() {
    const next = current();
    if (sameSelection(binding, next)) return visible;
    invalidate();
    if (next === null || reading || next.generation <= highestGeneration) return false;
    binding = Object.freeze(next);
    highestGeneration = next.generation;
    const ticket = transcript.begin();
    if (ticket === null) {
      invalidate();
      return false;
    }
    reading = true;
    let result = null;
    try { result = await readTranscript(ticket); }
    catch { /* Keep native fallback; don't display upstream errors. */ }
    finally { reading = false; }
    if (binding !== next) return false;
    if (!transcript.commit(ticket, result)) {
      invalidate();
      return false;
    }
    visible = true;
    composer?.refresh();
    if (selectedBinding() !== next) {
      if (binding === next) invalidate();
      return false;
    }
    // The fallback frame stays mounted with its original src and attributes.
    chat.setAttribute(activeAttribute, "");
    chat.setAttribute("aria-label", "Workspace conversation");
    center.hidden = false;
    return true;
  }

  return Object.freeze({ present, invalidate });
}
