// Explicit oc-box wrapper; standard browser events only, no native widget selectors.
// GPL-3.0-or-later.
(() => {
  "use strict";
  const SUPPORTED = new Set(["2.2.6", "2.3.0"]);
  const MAX_FILE = 20 * 1024 * 1024;
  const MAX_TOTAL = 40 * 1024 * 1024;
  const MAX_FILES = 8;

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

  function workspaceFrame(frame, origin, prefix) {
    const realm = frame.contentWindow;
    const url = new URL(realm.location.href);
    if (url.origin !== origin || !url.pathname.startsWith(prefix) ||
        !/^ses[A-Za-z0-9_-]+$/.test(url.pathname.slice(prefix.length))) {
      throw new Error("Open a native session on this workspace before adding attachments.");
    }
    return { realm, doc: realm.document, href: url.href };
  }

  function adapter(frame, origin, route, notice) {
    const prefix = route.slice(0, route.lastIndexOf("/") + 1);
    const forwarded = new WeakSet();
    const installed = new WeakSet();
    let busy = false;

    async function add(files, target) {
      if (busy) {
        notice("Another drop is being read. Wait for its thumbnails before adding more.");
        return;
      }
      busy = true;
      try {
        const before = workspaceFrame(frame, origin, prefix);
        const copies = await snapshot(files, before.realm);
        const after = workspaceFrame(frame, origin, prefix);
        if (before.doc !== after.doc || before.href !== after.href) {
          throw new Error("The native session changed while reading files. Nothing was forwarded; drop again.");
        }
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
          "Open a native session on this workspace before adding attachments.",
          "The native session changed while reading files. Nothing was forwarded; drop again.",
          "The original drop target is no longer available. Nothing was forwarded; drop again.",
        ].includes(_error.message);
        notice(allowed ? _error.message : "Attachment forwarding failed. Nothing was retried; check the native draft before trying again.");
      } finally {
        busy = false;
      }
    }

    function install() {
      const { doc } = workspaceFrame(frame, origin, prefix);
      if (installed.has(doc)) return;
      installed.add(doc);
      // Explicit wrapper-wide browser event normalization, not native private hooks.
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
    return { add, install };
  }

  async function boot() {
    const frame = document.getElementById("native");
    const status = document.getElementById("status");
    const notice = (text) => { status.textContent = text; };
    const route = frame.dataset.route;
    const files = document.getElementById("files");
    files.disabled = true;
    try {
      const reply = await fetch("/api/info", {
        credentials: "same-origin", cache: "no-store", redirect: "error",
      });
      if (!reply.ok || !SUPPORTED.has((await reply.json()).version)) {
        throw new Error("unsupported");
      }
      const current = adapter(frame, location.origin, route, notice);
      frame.addEventListener("load", () => {
        try {
          current.install();
          files.disabled = false;
          notice("Adapter active. Wait for the native composer, then drop files. Verify thumbnails before Send.");
        } catch (_error) {
          files.disabled = true;
          notice("Open the workspace session in the native frame before dropping files.");
        }
      });
      frame.src = route;
      const drop = document.getElementById("drop");
      drop.addEventListener("dragover", (event) => event.preventDefault());
      drop.addEventListener("drop", (event) => {
        event.preventDefault();
        if (!event.isTrusted) return;
        // No getData(), URL dereference, path lookup or clipboard access.
        void current.add(Array.from(event.dataTransfer.files));
      });
      files.addEventListener("change", (event) => {
        if (!event.isTrusted) return;
        const selected = Array.from(files.files);
        files.value = "";
        void current.add(selected);
      });
    } catch (_error) {
      notice("Adapter refused: authenticated native OpenCode 2.2.6 or 2.3.0 is required. Use the direct native link if appropriate.");
    }
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { snapshot, mime, hasFiles, workspaceFrame, adapter, MAX_FILE, MAX_TOTAL, MAX_FILES };
  } else {
    void boot();
  }
})();
