// Optional common-shell addon; standard File events, no native widget selectors.
// GPL-3.0-or-later.
(() => {
  "use strict";
  // Keep the exact native composer-test matrix in sync with web_attachments.py.
  const SUPPORTED = new Set(["2.2.6", "2.2.7", "2.3.0", "2.3.2", "2.4.1"]);
  const RELEASE_FALLBACK = "Use oc-box web without --attachments, or Ctrl+Y in the oc-box TUI with --host-clipboard where supported";
  const MAX_FILE = 20 * 1024 * 1024;
  const MAX_TOTAL = 40 * 1024 * 1024;
  const MAX_FILES = 8;
  const NATIVE_CHECK_MS = 5000;

  function mime(bytes) {
    const text = String.fromCharCode(...bytes);
    if ([137, 80, 78, 71, 13, 10, 26, 10].every((n, i) => bytes[i] === n)) return "image/png";
    if (bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255) return "image/jpeg";
    if (text.startsWith("GIF87a") || text.startsWith("GIF89a")) return "image/gif";
    if (text.startsWith("RIFF") && text.slice(8, 12) === "WEBP") return "image/webp";
    if (/^%PDF-[12]\.[0-9]/.test(text)) return "application/pdf";
    throw new Error("Use PNG, JPEG, GIF, WebP or PDF files with recognizable signatures.");
  }

  async function snapshot(files, realm) {
    if (!files.length || files.length > MAX_FILES) throw new Error("Choose one to eight image/PDF files.");
    let total = 0;
    for (const file of files) {
      if (!Number.isSafeInteger(file.size) || file.size <= 0 || file.size > MAX_FILE) {
        throw new Error("Each attachment must be nonempty and at most 20 MiB.");
      }
      total += file.size;
      if (total > MAX_TOTAL) throw new Error("A drop may contain at most 40 MiB.");
    }
    // Validate the entire batch before allocating snapshots or forwarding any file.
    const types = [];
    for (const file of files) types.push(mime(new Uint8Array(await file.slice(0, 12).arrayBuffer())));
    const result = [];
    for (let i = 0; i < files.length; i++) {
      const bytes = await files[i].arrayBuffer();
      if (bytes.byteLength !== files[i].size) throw new Error("The selected file changed; choose it again.");
      if (mime(new Uint8Array(bytes, 0, Math.min(12, bytes.byteLength))) !== types[i]) {
        throw new Error("The selected file changed; choose it again.");
      }
      const name = files[i].name.split(/[\\/]/).pop().replace(/[\x00-\x1f\x7f]/g, "_").slice(0, 255) || "attachment";
      result.push(new realm.File([bytes], name, { type: types[i] }));
    }
    return result;
  }

  function hasFiles(data) {
    return !!data && (data.files.length > 0 || Array.from(data.types).includes("Files"));
  }

  const UNBOUND = "Select a verified existing conversation in the common shell before adding attachments.";
  const CHANGED = "The native session changed while reading files. Nothing was forwarded; drop again.";

  function workspaceFrame(frame, origin, selection) {
    const selected = selection();
    if (!selected || !/^ses[A-Za-z0-9_-]{1,125}$/.test(selected.sessionID) ||
        typeof selected.nativeRoute !== "string" ||
        !selected.nativeRoute.endsWith("/session/" + selected.sessionID)) throw new Error(UNBOUND);
    const realm = frame.contentWindow;
    const url = new URL(realm.location.href);
    if (url.origin !== origin || url.pathname !== selected.nativeRoute ||
        !/^\/server\/[A-Za-z0-9_-]+\/session\/ses[A-Za-z0-9_-]+$/.test(selected.nativeRoute)) throw new Error(UNBOUND);
    return { realm, doc: realm.document, href: url.href,
      sessionID: selected.sessionID, route: selected.nativeRoute };
  }

  function adapter(frame, origin, selection, notice) {
    const forwarded = new WeakSet();
    const installed = new WeakSet();
    let document = null;
    let generation = 0;
    let enabled = true;
    let busy = false;

    function trackDocument(next) {
      if (document !== next) { document = next; generation += 1; }
    }
    function binding() {
      if (!enabled) throw new Error(UNBOUND);
      const current = workspaceFrame(frame, origin, selection);
      trackDocument(current.doc);
      return current;
    }
    function unchanged(before, epoch) {
      let after;
      try { after = binding(); } catch { throw new Error(CHANGED); }
      if (epoch !== generation || before.doc !== after.doc || before.href !== after.href ||
          before.sessionID !== after.sessionID || before.route !== after.route) throw new Error(CHANGED);
    }

    async function add(files, target) {
      if (busy) {
        notice("Another drop is being read. Wait for its thumbnails before adding more.");
        return;
      }
      busy = true;
      try {
        const before = binding();
        const epoch = generation;
        const copies = await snapshot(files, before.realm);
        unchanged(before, epoch);
        const data = new before.realm.DataTransfer();
        for (const file of copies) data.items.add(file);
        const event = new before.realm.DragEvent("drop", {
          dataTransfer: data, bubbles: true, cancelable: true,
        });
        forwarded.add(event);
        // The native application owns its document drop handler and all draft/Send
        // semantics. No DOM widget lookup, draft persistence editing or prompt API.
        if (target && target !== before.doc &&
            (target.ownerDocument !== before.doc || !target.isConnected)) {
          throw new Error("The original drop target is no longer available. Nothing was forwarded; drop again.");
        }
        const destination = target || before.doc;
        // Check the live public selection AND exact actual location immediately
        // before dispatch, including while SPA route verification is pending.
        unchanged(before, epoch);
        destination.dispatchEvent(event);
        notice(`Forwarded ${copies.length} file(s). Check native thumbnails before Send; the adapter has not submitted a prompt.`);
      } catch (_error) {
        // Fixed messages only; do not surface browser errors containing paths/data.
        const allowed = _error instanceof Error && [
          "Use PNG, JPEG, GIF, WebP or PDF files with recognizable signatures.",
          "Choose one to eight image/PDF files.",
          "Each attachment must be nonempty and at most 20 MiB.",
          "A drop may contain at most 40 MiB.",
          "The selected file changed; choose it again.",
          UNBOUND, CHANGED,
          "The original drop target is no longer available. Nothing was forwarded; drop again.",
        ].includes(_error.message);
        notice(allowed ? _error.message : "Attachment forwarding failed. Nothing was retried; check the native draft before trying again.");
      } finally {
        busy = false;
      }
    }

    function install() {
      const realm = frame.contentWindow;
      if (new URL(realm.location.href).origin !== origin) return;
      const doc = realm.document;
      trackDocument(doc);
      if (installed.has(doc)) return;
      installed.add(doc);
      // One handler per document, reading CURRENT public selection at each drop.
      // Rebinding a tab must not accumulate stale, session-specific interceptors.
      // Non-file native drags (including its workspace @mentions) stay unchanged.
      doc.addEventListener("dragover", (event) => {
        if (hasFiles(event.dataTransfer)) event.preventDefault();
      }, true);
      doc.addEventListener("drop", (event) => {
        if (forwarded.has(event) || !hasFiles(event.dataTransfer)) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        if (!event.isTrusted) {
          notice("Use an actual browser file drop or the Choose attachments control.");
          return;
        }
        void add(Array.from(event.dataTransfer.files), event.target);
      }, true);
    }
    function invalidate() {
      generation += 1; // Every selection event counts, even same-ID/away-and-back.
      try { install(); } catch { /* Cross-origin/unavailable documents stay unbound. */ }
    }
    return {
      add, install, invalidate,
      setEnabled(value) { enabled = value; invalidate(); },
      available() { try { binding(); return true; } catch { return false; } },
    };
  }

  async function boot(hostWindow, hostDocument, request) {
    const shell = hostWindow.ocBoxWebShell;
    if (!shell || shell.version !== 1 || !Object.isFrozen(shell) ||
        !shell.frame || !shell.controls || !shell.status || typeof shell.selection !== "function") return;
    const notice = text => { shell.status.textContent = text; };
    const panel = hostDocument.createElement("section");
    const title = hostDocument.createElement("strong");
    title.textContent = "oc-box attachments — files only; no automatic prompt Send";
    const drop = hostDocument.createElement("p");
    drop.textContent = "Drop PNG/JPEG/GIF/WebP/PDF here or into the native composer, or choose files: ";
    const files = hostDocument.createElement("input");
    files.type = "file";
    files.multiple = true;
    files.accept = "image/png,image/jpeg,image/gif,image/webp,application/pdf";
    files.setAttribute("aria-label", "Choose image or PDF attachments");
    files.disabled = true;
    drop.append(files);
    const details = hostDocument.createElement("details");
    const summary = hostDocument.createElement("summary");
    summary.textContent = "Attachment behavior and privacy";
    const privacy = hostDocument.createElement("p");
    privacy.textContent = "20 MiB per file; eight files and 40 MiB per drop. Signatures are not a full safety scan. " +
      "No host paths/URLs, DOCX conversion or automatic clipboard reads. Native may stage bytes in its container before Send. " +
      "Your selected native model must support images/PDF for inline input. Browser drafts (including IndexedDB), " +
      "container history and provider disclosure still apply; removing a thumbnail is not guaranteed erasure. " +
      "Wait for the native composer and verify thumbnails before native Send. No prompt API or retries are added.";
    details.append(summary, privacy);
    panel.append(title, drop, details);
    shell.controls.append(panel);
    const current = adapter(shell.frame, hostWindow.location.origin, shell.selection, notice);
    current.setEnabled(false);
    let active = false;
    const stateChanged = () => {
      current.invalidate();
      files.disabled = !active || !current.available();
      if (active) notice(files.disabled ? UNBOUND :
        "Adapter active on the selected conversation. Wait for its native composer; verify thumbnails before Send.");
    };
    hostWindow.addEventListener("oc-box:selection-changed", stateChanged);
    hostWindow.addEventListener("oc-box:frame-ready", stateChanged);
    // Invalidate immediately on document load, before any async common-shell
    // route verification/frame-ready event can complete.
    shell.frame.addEventListener("load", stateChanged);
    drop.addEventListener("dragover", event => event.preventDefault());
    drop.addEventListener("drop", event => {
      event.preventDefault();
      if (!event.isTrusted) return;
      void current.add(Array.from(event.dataTransfer.files));
    });
    files.addEventListener("change", event => {
      if (!event.isTrusted) return;
      const selected = Array.from(files.files);
      files.value = "";
      void current.add(selected);
    });
    notice("Checking native attachment version…");
    const check = new AbortController();
    const timeout = setTimeout(() => check.abort(), NATIVE_CHECK_MS);
    let refusal = "the private native service could not be verified";
    try {
      const reply = await request("/api/info", {
        credentials: "same-origin", cache: "no-store", redirect: "error", signal: check.signal,
      });
      if (reply.status === 401 || reply.status === 403) {
        refusal = "native browser authentication failed; open a fresh oc-box web link";
      }
      if (!reply.ok) throw new Error("unavailable");
      const info = await reply.json();
      if (typeof info?.version !== "string") throw new Error("unavailable");
      if (!SUPPORTED.has(info.version)) {
        refusal = `the native browser composer contract is not verified for this release. ${RELEASE_FALLBACK}`;
        throw new Error("unsupported");
      }
      active = true;
      current.setEnabled(true);
      stateChanged();
    } catch (_error) {
      current.setEnabled(false);
      notice(`Attachment adapter refused: ${refusal}. Native shell navigation remains available.`);
    } finally {
      clearTimeout(timeout);
    }
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { snapshot, mime, hasFiles, workspaceFrame, adapter, boot, MAX_FILE, MAX_TOTAL, MAX_FILES };
  } else {
    void boot(window, document, fetch);
  }
})();
