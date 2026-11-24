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
 * Trusted bootstrap only, after the corresponding shell and Studio CSS are ready.
 * The controller synchronously supplies {sessionID, generation, writable} or
 * null, reads a bounded transcript for {sessionID, generation}, and may send
 * {sessionID, generation, text}. A writable selection requires an admitted
 * send function. The shell's selected conversation is a cross-check, not
 * authority, both for the native iframe and the iframe-free named chrome.
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
  const named = root.dataset.namedStudio === "";
  const shell = named ? window?.ocBoxNamedStudioChrome : window?.ocBoxWebShell;
  const chat = root.querySelector(":scope > #workspace-content > #workspace-chat");
  const frame = chat?.querySelector(":scope > #native");
  const center = chat?.querySelector(":scope > #workspace-studio-center");
  const transcriptSlot = center?.querySelector(":scope > #workspace-studio-transcript");
  const composerSlot = center?.querySelector(":scope > #workspace-studio-composer");
  if (document.getElementById("web-shell") !== root || !root.isConnected ||
      (named ? root.querySelector("iframe") : !frame || shell?.frame !== frame) ||
      shell?.version !== 1 ||
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
    if (!root.isConnected || (named ? window.ocBoxNamedStudioChrome : window.ocBoxWebShell) !== shell ||
        !center.isConnected || document.hidden || chat.hidden ||
        (named ? root.querySelector("iframe") : frame.hidden)) return null;
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
    catch { /* Keep fallback; don't display upstream errors. */ }
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
    // On the ordinary page the fallback stays mounted with its original attributes.
    chat.setAttribute(activeAttribute, "");
    chat.setAttribute("aria-label", "Workspace conversation");
    center.hidden = false;
    return true;
  }

  async function refresh(scope) {
    if (!visible || reading) return false;
    const expected = selectedBinding();
    if (expected === null || scope?.sessionID !== expected.sessionID ||
        scope.generation !== expected.generation) return false;
    const ticket = transcript.beginRefresh();
    if (ticket === null) return false;
    reading = true;
    try {
      let result = null;
      try { result = await readTranscript(ticket); }
      catch { /* Keep the last valid transcript for a still-current scope. */ }
      if (binding !== expected) return false;
      if (selectedBinding() !== expected) {
        if (binding === expected) invalidate();
        return false;
      }
      // This never remounts, refreshes or focuses the separate composer.
      return transcript.commit(ticket, result);
    } finally {
      reading = false;
    }
  }

  return Object.freeze({ present, refresh, invalidate });
}
