// Common shell v1. The native app and its composer are opaque: never inspect,
// inject into, record or serialize iframe DOM, browser stores or draft values.
(() => {
  "use strict";
  const root = document.getElementById("web-shell");
  if (!root) return;
  const tabs = document.getElementById("session-tabs");
  const frame = document.getElementById("native");
  const retained = document.getElementById("retained-selection");
  const notice = document.getElementById("selection-status");
  const status = document.getElementById("catalog-status");
  const empty = document.getElementById("no-selection");
  const refresh = document.getElementById("refresh-tabs");
  const endpoint = "/auth/oc-box-sessions/catalog";
  const verifyEndpoint = "/auth/oc-box-sessions/selection";
  const idPattern = /^ses[A-Za-z0-9_-]{1,125}$/;
  let members = [];
  let selected = null;
  let busy = false;
  let nativePrefix = "";
  let awaitingLoad = false;
  let observedWindow = null;
  let timer = null;
  let navigation = 0;
  let sawFrameLoad = false;
  let membershipKnown = false;

  function row(value) {
    if (!value || typeof value.id !== "string" || !idPattern.test(value.id) || typeof value.title !== "string" ||
        typeof value.route !== "string" ||
        !/^\/server\/[A-Za-z0-9_-]+\/session\/ses[A-Za-z0-9_-]+$/.test(value.route) ||
        !value.route.endsWith("/session/" + value.id) ||
         (nativePrefix && value.route !== nativePrefix + value.id) ||
         (value.activeRoot != null && (typeof value.activeRoot !== "string" ||
           !idPattern.test(value.activeRoot)))) throw new Error("catalog");
    return { id: value.id, title: value.title.slice(0, 256), route: value.route,
      activeRoot: value.activeRoot ?? null };
  }
  function selection() {
    if (!selected || awaitingLoad) return null;
    try {
      const url = new URL(frame.contentWindow.location.href);
      if (url.origin !== window.location.origin || url.pathname !== selected.route) return null;
    } catch { return null; }
    return Object.freeze({ sessionID: selected.id, nativeRoute: selected.route,
      activeRoot: membershipKnown && members.some(item => item.id === selected.activeRoot) ? selected.activeRoot : null,
      member: selected.member, origin: selected.origin });
  }
  function changed() {
    // Attachment add-ons may mount controls in #session-controls and use this
    // exact iframe. No nested shell, second strip or native-store mutation.
    window.dispatchEvent(new CustomEvent("oc-box:selection-changed", {
      detail: selection()
    }));
  }
  function render() {
    const current = awaitingLoad ? selected : selection();
    const activeId = membershipKnown && current && members.some(item => item.id === current.activeRoot) ?
      current.activeRoot : awaitingLoad ? selected?.id : current?.sessionID;
    tabs.replaceChildren();
    for (const item of members) {
      const button = document.createElement("button");
      button.type = "button";
      button.setAttribute("role", "tab");
      button.setAttribute("aria-controls", "native");
      button.setAttribute("aria-selected", String(activeId === item.id));
      button.textContent = item.title || item.id;
      button.title = item.id;
      button.addEventListener("click", () => select(item, "Explicit TUI-tab click"));
      tabs.append(button);
    }
    retained.replaceChildren();
    if (selected) {
      selected.member = membershipKnown && members.some(item => item.id === selected.id);
      const childOfOpenTab = membershipKnown && !selected.member &&
        members.some(item => item.id === selected.activeRoot);
      notice.textContent = selected.origin + (selected.member ? "" : childOfOpenTab ?
        " — viewing a child of open TUI tab " + selected.activeRoot :
        membershipKnown ? " — selection is not in the latest TUI membership; conversation retained" :
          " — TUI membership is unavailable; conversation retained");
      if (!selected.member) {
        const label = document.createElement("p");
        label.textContent = (childOfOpenTab ? "Child conversation in the highlighted TUI tab: " :
          "Retained browser selection (not counted as TUI-open): ") + selected.title + " · " + selected.id;
        retained.append(label);
      }
    } else notice.textContent = "No native conversation is currently verified. Choose a tab or use native Home.";
    status.textContent = (membershipKnown ? members.length + " verified open TUI tab(s). " :
      "TUI membership unavailable; open count unknown. ") +
      "Refresh does not reload the selected conversation or verify native focus.";
  }
  function select(item, origin) {
    const next = row(item);
    navigation += 1;
    selected = { ...next, origin, member: members.some(value => value.id === next.id),
      activeRoot: next.activeRoot ?? (members.some(value => value.id === next.id) ? next.id : null) };
    let differs = frame.getAttribute("src") !== next.route;
    try {
      const url = new URL(frame.contentWindow.location.href);
      if (url.href !== "about:blank" &&
          (url.origin !== window.location.origin || url.pathname !== next.route)) differs = true;
      else if (!awaitingLoad && url.href !== "about:blank") differs = false;
    } catch { differs = true; }
    if (differs) {
      awaitingLoad = true;
      frame.setAttribute("src", next.route);
    }
    frame.hidden = false;
    empty.hidden = true;
    const url = new URL(window.location.href);
    url.searchParams.set("session", next.id);
    window.history.replaceState(null, "", url.pathname + url.search);
    render();
    changed();
  }
  function unboundView() {
    if (!selected) return;
    selected = null;
    navigation += 1;
    const url = new URL(window.location.href);
    url.searchParams.delete("session");
    window.history.replaceState(null, "", url.pathname + url.search);
    render(); // Keep the native view/draft intact, but assert no selected TUI ID.
    changed();
  }
  async function observeNativeRoute() {
    if (awaitingLoad || frame.hidden || document.hidden) return;
    let url;
    try { url = new URL(frame.contentWindow.location.href); }
    catch { unboundView(); return; }
    if (url.href === "about:blank") return;
    if (url.origin !== window.location.origin || !url.pathname.startsWith(nativePrefix)) {
      unboundView(); return;
    }
    const id = url.pathname.slice(nativePrefix.length);
    if (!idPattern.test(id)) { unboundView(); return; }
    if (selected?.id === id && selected.route === url.pathname) return;
    unboundView(); // Never keep highlighting a different iframe conversation.
    if (busy) return;
    // URL observation is not native TUI focus evidence. Verify existing identity
    // against this pinned service before updating the outer selected-tab claim.
    busy = true;
    const revision = navigation;
    try {
      const response = await fetch(verifyEndpoint + "?session=" + encodeURIComponent(id),
        { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error("selection");
      const item = row(await response.json());
      if (item.id !== id || item.route !== url.pathname) throw new Error("selection");
      // Recheck after await: the user may have navigated again during verification.
      if (revision !== navigation || awaitingLoad ||
          frame.contentWindow.location.href !== url.href) return;
      selected = { ...item, origin: "Native iframe navigation; not verified TUI focus",
        member: members.some(value => value.id === id) };
      const outer = new URL(window.location.href);
      outer.searchParams.set("session", id);
      window.history.replaceState(null, "", outer.pathname + outer.search);
      render();
      changed(); // Never assign iframe.src while observing native SPA navigation.
    } catch {
      if (revision === navigation && !awaitingLoad) {
        unboundView();
        notice.textContent = "Native iframe selection could not be verified; no active TUI tab is asserted.";
      }
    }
    finally { busy = false; }
  }
  function frameReady() {
    awaitingLoad = false;
    try {
      if (observedWindow) {
        observedWindow.removeEventListener("popstate", observeNativeRoute);
        observedWindow.removeEventListener("hashchange", observeNativeRoute);
      }
      observedWindow = frame.contentWindow;
      observedWindow.addEventListener("popstate", observeNativeRoute);
      observedWindow.addEventListener("hashchange", observeNativeRoute);
    } catch { observedWindow = null; }
    void observeNativeRoute().finally(() => {
      render();
      window.dispatchEvent(new CustomEvent("oc-box:frame-ready", { detail: selection() }));
    });
  }
  async function refreshTabs() {
    if (busy) return;
    busy = true;
    refresh.disabled = true;
    try {
      const response = await fetch(endpoint, { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error("catalog");
      const data = await response.json();
      if (!Array.isArray(data.sessions)) throw new Error("catalog");
      const next = data.sessions.map(row);
      if (new Set(next.map(item => item.id)).size !== next.length) throw new Error("catalog");
      members = next;
      membershipKnown = data.status === "valid";
      render(); // Deliberately never select/reload a frame on metadata refresh.
      changed();
    } catch {
      membershipKnown = false;
      render();
      changed();
      status.textContent = "TUI membership could not be refreshed. Conversation retained; retry or open native Home.";
    } finally {
      busy = false;
      refresh.disabled = false;
    }
  }
  try {
    const data = JSON.parse(root.dataset.bootstrap);
    if (typeof data.nativePrefix !== "string" ||
        !/^\/server\/[A-Za-z0-9_-]+\/session\/$/.test(data.nativePrefix)) throw new Error("prefix");
    nativePrefix = data.nativePrefix;
    membershipKnown = data.status === "valid";
    members = data.sessions.map(row);
    if (new Set(members.map(item => item.id)).size !== members.length) throw new Error("catalog");
    if (data.selection) {
      selected = { ...row(data.selection), origin: data.selection.origin,
        member: members.some(item => item.id === data.selection.id) };
    }
    notice.textContent = data.selectionNotice;
    render();
    refresh.addEventListener("click", refreshTabs);
    frame.addEventListener("load", () => { sawFrameLoad = true; frameReady(); });
    window.addEventListener("load", () => { if (!sawFrameLoad) frameReady(); }, { once: true });
    window.ocBoxWebShell = Object.freeze({
      version: 1, frame, controls: document.getElementById("session-controls"),
      status: document.getElementById("addon-status"), selection, refresh: refreshTabs
    });
    if (selected) select(selected, selected.origin); // Existing src is not reassigned.
    const startObserver = () => {
      if (timer === null) timer = window.setInterval(() => { void observeNativeRoute(); }, 1000);
    };
    startObserver();
    window.addEventListener("pagehide", () => { window.clearInterval(timer); timer = null; });
    window.addEventListener("pageshow", () => { startObserver(); void observeNativeRoute(); });
  } catch {
    status.textContent = "The shell could not initialize. Use explicit fallback links or native Home.";
  }
})();
