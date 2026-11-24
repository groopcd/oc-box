// Common shell v1. The native app and its composer are opaque: never inspect,
// inject into, record or serialize iframe DOM, browser stores or draft values.
(() => {
  "use strict";
  const root = document.getElementById("web-shell");
  if (!root) return;
  const byId = id => document.getElementById("workspace-" + id);
  const tabs = document.getElementById("session-tabs");
  const groups = {
    tabs: { list: tabs, panel: byId("open-group") },
    history: { list: byId("history-list"), panel: byId("history-group") },
    archived: { list: byId("archived-list"), panel: byId("archived-group") },
  };
  const frame = document.getElementById("native");
  const retained = document.getElementById("retained-selection");
  const notice = document.getElementById("selection-status");
  const status = document.getElementById("catalog-status");
  const empty = document.getElementById("no-selection");
  const refresh = document.getElementById("refresh-tabs");
  const conversationTitle = byId("conversation-title");
  const conversationSource = byId("conversation-source");
  const currentSession = byId("current-session");
  const endpoint = "/auth/oc-box-sessions/catalog";
  const verifyEndpoint = "/auth/oc-box-sessions/selection";
  const historyEndpoint = "/auth/oc-box-sessions/history";
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
  let workspace = null;
  let ready = false;

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
    if (!selected || awaitingLoad || workspace.manager()) return null;
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
  function rowIcon(name) {
    const namespace = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(namespace, "svg");
    const use = document.createElementNS(namespace, "use");
    svg.setAttribute("class", "workspace-icon");
    svg.setAttribute("aria-hidden", "true");
    use.setAttribute("href", "#i-" + name);
    svg.append(use);
    return svg;
  }
  function render() {
    const current = awaitingLoad ? selected : selection();
    const currentId = awaitingLoad ? selected?.id : current?.sessionID;
    const openIds = new Set(membershipKnown ? members.map(item => item.id) : []);
    const activeId = openIds.has(current?.activeRoot) ? current.activeRoot : currentId;
    const focused = document.activeElement?.dataset;
    const source = workspace.source();
    const isTabs = source === "tabs";
    let refocus;
    for (const [kind, { list, panel }] of Object.entries(groups)) {
      list.replaceChildren();
      panel.hidden = kind !== source;
    }
    for (const item of workspace.visibleRows()) {
      const kind = isTabs ? "tabs" : item.archived ? "archived" : "history";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "workspace-session" + (item.child ? " workspace-child" : "");
      const title = item.title || item.id;
      if (workspace.manager()) {
        button.setAttribute("aria-label", "Open in native chat: " + title);
      } else if (isTabs) {
        button.setAttribute("role", "tab");
        button.setAttribute("aria-controls", "native");
        button.setAttribute("aria-label", title);
        button.setAttribute("aria-selected", String(activeId === item.id));
      }
      if (!isTabs) button.setAttribute("aria-current", String(currentId === item.id));
      const copy = document.createElement("span");
      const titleNode = document.createElement("span");
      titleNode.className = "workspace-session-title";
      titleNode.setAttribute("dir", "auto");
      titleNode.textContent = title;
      const description = document.createElement("small");
      description.textContent = isTabs ? "Open TUI tab" :
        [item.child ? "Child session" : null, item.archived ? "Archived" : null,
          openIds.has(item.id) ? "In open TUI tabs" : null]
          .filter(Boolean).join(" · ") || "Workspace history";
      copy.append(titleNode, description);
      button.append(rowIcon(item.child ? "child" : item.archived ? "archive" : "chat"), copy);
      button.title = item.id;
      button.dataset.sessionId = item.id;
      button.dataset.sessionSource = source;
      button.addEventListener("click", () => workspace.open(item));
      groups[kind].panel.hidden = false;
      groups[kind].list.append(button);
      if (focused?.sessionId === item.id && focused.sessionSource === source) refocus = button;
    }
    refocus?.focus({ preventScroll: true });
    retained.replaceChildren();
    conversationTitle.textContent = selected?.title || "Native conversation";
    conversationSource.textContent = "OpenCode / Native conversation";
    currentSession.textContent = selected ? (selected.title || selected.id) + " · " + selected.id :
      "No conversation selected.";
    if (selected) {
      selected.member = openIds.has(selected.id);
      const childOfOpenTab = !selected.member && openIds.has(selected.activeRoot);
      const sourceLabel = selected.historyKind === "archived" ? "Archived in workspace" :
        selected.historyKind === "history" ? "Workspace history" :
        selected.member ? "Open TUI tab" : childOfOpenTab ? "Child of open TUI tab" : "Browser selection";
      conversationSource.textContent = "OpenCode / " + sourceLabel;
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
  function select(item, origin, historyKind = null) {
    const next = row(item);
    navigation += 1;
    selected = { ...next, origin, historyKind, member: members.some(value => value.id === next.id),
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
    if (awaitingLoad || frame.hidden || document.hidden || workspace.manager()) return;
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
    finally {
      busy = false;
      refresh.disabled = workspace.isHistoryLoading();
    }
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
      members = []; // A failed refresh cannot leave a stale count or action.
      render();
      changed();
      status.textContent = "TUI membership could not be refreshed. Conversation retained; retry or open native Home.";
    } finally {
      busy = false;
      refresh.disabled = workspace.isHistoryLoading();
    }
  }
  function studioDividers(content, wide) {
    const minPane = 208; // Must agree with the wide Studio CSS tracks.
    const minChat = 420;
    const gutter = 10;
    const defaults = { left: 248, right: 278 };
    const requested = { ...defaults };
    const widths = {};
    const handles = {
      left: document.getElementById("workspace-divider-left"),
      right: document.getElementById("workspace-divider-right"),
    };
    const other = side => side === "left" ? "right" : "left";
    const budget = () => Math.max(2 * minPane, content.clientWidth - minChat - 2 * gutter);
    const clamp = (value, max) => Math.min(Math.max(value, minPane), max);
    let priority = "left";
    let drag = null;
    const paired = () => root.dataset.layout === "studio" && root.dataset.workspaceInspector === "true";

    function cancel() {
      if (!drag) return;
      const { handle, id } = drag;
      drag = null;
      delete root.dataset.resizing;
      if (handle.hasPointerCapture(id)) handle.releasePointerCapture(id);
    }
    function fit() {
      const enabled = ["studio", "dashboard"].includes(root.dataset.layout) && wide.matches;
      handles.left.hidden = !enabled;
      handles.right.hidden = !enabled || !paired();
      if (!enabled) { cancel(); return; }
      if (!paired()) {
        const maximum = Math.max(minPane, content.clientWidth - minChat - gutter);
        widths.left = clamp(requested.left, maximum);
        root.style.setProperty("--workspace-left", widths.left + "px");
        handles.left.setAttribute("aria-valuemin", String(minPane));
        handles.left.setAttribute("aria-valuemax", String(maximum));
        handles.left.setAttribute("aria-valuenow", String(widths.left));
        return;
      }
      const available = budget();
      const secondary = other(priority);
      // The moved pane can borrow room from its neighbor, but not from chat's
      // minimum width. Keep the other preference for when space comes back.
      widths[priority] = clamp(requested[priority], available - minPane);
      widths[secondary] = clamp(requested[secondary], available - widths[priority]);
      for (const side of ["left", "right"]) {
        root.style.setProperty("--workspace-" + side, widths[side] + "px");
        handles[side].setAttribute("aria-valuemin", String(minPane));
        handles[side].setAttribute("aria-valuemax", String(available - minPane));
        handles[side].setAttribute("aria-valuenow", String(widths[side]));
      }
    }
    function resize(side, value) {
      const limit = paired() ? budget() - minPane : Math.max(minPane, content.clientWidth - minChat - gutter);
      requested[side] = clamp(value, limit);
      priority = side;
      fit();
    }
    for (const side of ["left", "right"]) {
      const handle = handles[side];
      const direction = side === "left" ? 1 : -1;
      handle.addEventListener("pointerdown", event => {
        if (!ready || handle.hidden || drag || event.button !== 0 || !event.isPrimary) return;
        handle.setPointerCapture(event.pointerId);
        drag = { handle, id: event.pointerId, startX: event.clientX, startWidth: widths[side] };
        root.dataset.resizing = side;
        handle.focus();
        event.preventDefault();
      });
      handle.addEventListener("pointermove", event => {
        if (drag?.handle !== handle || drag.id !== event.pointerId) return;
        resize(side, drag.startWidth + direction * (event.clientX - drag.startX));
      });
      for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) {
        handle.addEventListener(type, event => {
          if (drag?.handle === handle && drag.id === event.pointerId) cancel();
        });
      }
      handle.addEventListener("dblclick", () => { if (ready && !handle.hidden) resize(side, defaults[side]); });
      handle.addEventListener("keydown", event => {
        if (!ready || handle.hidden) return;
        const step = event.shiftKey ? 64 : 16;
        let next;
        if (event.key === "ArrowLeft") next = widths[side] - direction * step;
        else if (event.key === "ArrowRight") next = widths[side] + direction * step;
        else if (event.key === "Home") next = minPane;
        else if (event.key === "End") next = Number(handle.getAttribute("aria-valuemax"));
        else if (event.key === "Enter") next = defaults[side];
        else return;
        event.preventDefault();
        resize(side, next);
      });
    }
    const refit = () => { if (ready) fit(); };
    window.addEventListener("resize", refit);
    return { fit, cancel };
  }
  function workspaceLayout() {
    const buttons = id => Array.from(byId(id).querySelectorAll("button"));
    const views = buttons("layouts");
    const sections = buttons("manager-nav");
    const search = byId("search");
    const library = byId("library");
    const toggles = { sessions: byId("sessions-toggle"), inspector: byId("inspector-toggle") };
    const overview = byId("overview"), overviewTitle = byId("overview-title"), chat = byId("chat");
    const sessionsClose = byId("sessions-close");
    const inspectorClose = byId("inspector-close"), inspectorLink = byId("inspector-link");
    const roomy = window.matchMedia("(min-width: 681px)");
    const wide = window.matchMedia("(min-width: 1001px)");
    const sources = { tabs: byId("open-tabs"), history: byId("history-tabs"), archived: byId("archived-tabs") };
    const dividers = studioDividers(byId("content"), wide);
    const layouts = {
      studio: ["Studio.", "Sessions, chat and workspace context."],
      dashboard: ["Dashboard.", "Workspace status first, with the selected conversation underneath."],
      focus: ["Chat focus.", "More room for the conversation; open sessions or the inspector on demand."],
      manager: ["Workspace manager.", "Browse status and sessions without chat. Host controls stay local."],
    };
    let view = "studio";
    let lastChat = view;
    let section = "status";
    let panel = null;
    let dockedInspector = true;
    let inspectorReturn = null;
    let source = "tabs", historyState = "idle", historyRows = [];
    let historyController, verifyingHistory = false;
    const manager = () => view === "manager";
    const sessionsDrawer = () => !manager() && (view === "focus" || !roomy.matches);
    const inspectorDrawer = () => view === "focus" || (view === "studio" && !wide.matches);
    const historySource = () => source !== "tabs";

    async function loadHistory() {
      historyController?.abort();
      const controller = new AbortController();
      historyController = controller;
      historyState = "loading";
      historyRows = [];
      if (historySource()) {
        refresh.disabled = true;
        render();
      }
      try {
        const response = await fetch(historyEndpoint, {
          credentials: "same-origin", headers: { Accept: "application/json" },
          signal: controller.signal,
        });
        if (!response.ok) throw new Error("history");
        const values = await response.json();
        if (!Array.isArray(values) || values.length > 1000) throw new Error("history");
        const received = values.map(value => {
          if (!value || typeof value.id !== "string" || !idPattern.test(value.id) ||
              typeof value.title !== "string" || typeof value.isChild !== "boolean" ||
              !value.time || typeof value.time !== "object") throw new Error("history");
          return { ...row({ id:value.id, title:value.title, route:nativePrefix + value.id }),
            archived:value.time.archived !== undefined, child:value.isChild };
        });
        if (new Set(received.map(item => item.id)).size !== received.length) throw new Error("history");
        if (historyController !== controller) return;
        historyRows = received;
        historyState = "valid";
      } catch {
        if (historyController !== controller) return;
        historyState = "unavailable";
      } finally {
        if (historyController === controller) {
          historyController = null;
          if (historySource()) {
            refresh.disabled = false;
            render();
          }
        }
      }
    }
    function setSource(value) {
      if (source !== value) navigation += 1; // An away/back source change invalidates pending opens.
      source = value;
      for (const [kind, button] of Object.entries(sources)) {
        button.setAttribute("aria-pressed", String(value === kind));
      }
      const label = historySource() ? "Refresh workspace history" : "Refresh TUI tabs";
      refresh.setAttribute("aria-label", label);
      refresh.title = label;
      refresh.disabled = historySource() ? historyState === "loading" : busy;
      render();
      if (historySource() && historyState === "idle") void loadHistory();
    }
    function apply() {
      root.dataset.layout = view;
      const [name, description] = layouts[view];
      byId("layout-name").textContent = name;
      byId("layout-description").textContent = description +
        (view === "studio" && wide.matches ? " Drag the dividers, or focus one and use the arrow keys." : "");
      for (const button of views) button.setAttribute("aria-pressed", String(button.dataset.view === view));
      for (const button of sections) button.setAttribute("aria-pressed", String(button.dataset.section === section));
      byId("manager-nav").hidden = !manager();
      chat.hidden = manager(); // Never move the frame or write its src here.
      chat.inert = panel !== null;
      overview.hidden = manager() ? section !== "status" : inspectorDrawer() ? panel !== "inspector" :
        view === "studio" && !dockedInspector;
      library.hidden = manager() ? section !== "sessions" : sessionsDrawer() && panel !== "sessions";
      library.inert = panel === "inspector";
      tabs.setAttribute("role", manager() ? "group" : "tablist");
      root.dataset.workspaceSessionsDrawer = String(sessionsDrawer());
      root.dataset.workspaceInspectorDrawer = String(inspectorDrawer());
      root.dataset.workspaceInspector = String(!overview.hidden);
      toggles.sessions.hidden = !sessionsDrawer();
      sessionsClose.hidden = !sessionsDrawer();
      toggles.sessions.setAttribute("aria-expanded", String(panel === "sessions"));
      toggles.inspector.hidden = manager() || view === "dashboard";
      toggles.inspector.setAttribute("aria-expanded", String(!overview.hidden));
      inspectorClose.hidden = toggles.inspector.hidden;
      inspectorLink.setAttribute("aria-expanded", String(!overview.hidden));
      byId("inspector-context").textContent = manager() || view === "dashboard" ?
        "Workspace overview" : "Workspace inspector";
      dividers.fit();
    }
    function show(next) {
      if (view === next) return;
      if (!manager()) lastChat = view;
      dividers.cancel();
      view = next;
      panel = null;
      dockedInspector = true;
      inspectorReturn = null;
      apply();
      navigation += 1; // Invalidate outstanding native route checks as well.
      render();
      changed(); // Invalidate File reads even for a round trip to the same selection.
      if (!manager()) void observeNativeRoute();
    }
    function toggleSessions() {
      if (!sessionsDrawer()) return;
      navigation += 1;
      panel = panel === "sessions" ? null : "sessions";
      apply();
      (panel ? search : toggles.sessions).focus();
    }
    function showInspector(open, trigger = toggles.inspector) {
      if (manager()) return;
      if (view === "dashboard") {
        // The dashboard already shows status above chat. Close a narrow
        // session drawer before taking focus there.
        if (panel === "sessions") toggleSessions();
        overviewTitle.focus();
        return;
      }
      navigation += 1;
      dividers.cancel();
      if (open) inspectorReturn = trigger === inspectorLink && sessionsDrawer() ? toggles.sessions : trigger;
      if (inspectorDrawer()) panel = open ? "inspector" : null;
      else dockedInspector = open;
      apply();
      let returnTo = inspectorReturn || toggles.inspector;
      if (returnTo === inspectorLink && library.hidden) returnTo = toggles.sessions;
      (open ? overviewTitle : returnTo.hidden ? toggles.inspector : returnTo).focus();
    }
    const on = (target, type, listener) =>
      target.addEventListener(type, event => { if (ready) listener(event); });
    for (const button of views) on(button, "click", () => show(button.dataset.view));
    for (const button of sections) on(button, "click", () => {
      if (section !== button.dataset.section) navigation += 1;
      section = button.dataset.section;
      apply();
    });
    on(toggles.sessions, "click", toggleSessions);
    on(sessionsClose, "click", toggleSessions);
    on(toggles.inspector, "click", () => showInspector(overview.hidden));
    on(inspectorClose, "click", () => showInspector(false));
    on(inspectorLink, "click", () => showInspector(true, inspectorLink));
    on(root, "keydown", event => {
      if (panel && event.key === "Escape" && !event.defaultPrevented) {
        event.preventDefault();
        if (panel === "sessions") toggleSessions();
        else showInspector(false);
      }
    });
    function fitPanes() {
      const focused = document.activeElement;
      if (panel === "sessions" && !sessionsDrawer()) panel = null;
      if (panel === "inspector" && !inspectorDrawer()) panel = null;
      if (!panel && focused) {
        if (sessionsDrawer() && library.contains(focused)) panel = "sessions";
        else if (inspectorDrawer() && overview.contains(focused)) panel = "inspector";
      }
      if (view === "studio" && wide.matches && focused && overview.contains(focused)) dockedInspector = true;
      apply();
    }
    on(roomy, "change", fitPanes);
    on(wide, "change", fitPanes);
    for (const [kind, button] of Object.entries(sources)) on(button, "click", () => setSource(kind));
    on(search, "input", render);
    apply();
    return {
      manager,
      source: () => source,
      isHistoryLoading: () => historySource() && historyState === "loading",
      refresh: () => historySource() ? loadHistory() : refreshTabs(),
      cancel() {
        if (historyController) {
          historyController.abort();
          historyController = null;
          historyState = "idle";
          refresh.disabled = false;
          if (historySource()) render();
        }
      },
      visibleRows() {
        const query = search.value.slice(0, 128).trim().toLowerCase();
        const history = historySource();
        const list = source === "archived" ? historyRows.filter(item => item.archived) :
          history ? historyRows : membershipKnown ? members : [];
        const visible = list.filter(item =>
          !query || item.title.toLowerCase().includes(query) || item.id.toLowerCase().includes(query));
        byId("search-status").textContent = history ?
          historyState === "loading" ? "Loading workspace history…" :
          historyState !== "valid" ? "Workspace history unavailable. Refresh to retry." :
          list.length === 0 ? (source === "archived" ? "No archived sessions in this workspace project." :
            "No sessions in this workspace project.") :
          visible.length === 0 ? "No workspace history matches this search." :
          `${visible.length} of ${list.length} ${source === "archived" ? "archived" : "workspace history"} rows. Not TUI membership.` :
          !membershipKnown ? "Verified open TUI sessions unavailable." :
          members.length === 0 ? "No verified open TUI sessions." :
          visible.length === 0 ? "No open TUI sessions match this search." :
          `${visible.length} of ${members.length} open TUI tabs.` + (manager() ? " Choose a tab to open native chat." : "");
        return visible;
      },
      async open(item) {
        if (!ready) return;
        if (historySource()) {
          if (verifyingHistory || historyState !== "valid" || !historyRows.includes(item)) return;
          verifyingHistory = true;
          const before = navigation;
          const snapshot = historyRows;
          try {
            const response = await fetch(historyEndpoint + "?session=" + encodeURIComponent(item.id), {
              credentials:"same-origin", headers:{ Accept:"application/json" },
            });
            if (!response.ok) throw new Error("selection");
            const result = await response.json();
            if (!result || result.id !== item.id) throw new Error("selection");
            const checked = row({ ...result, route:nativePrefix + result.id });
            if (before !== navigation || !historySource() || snapshot !== historyRows) return;
            select(checked, "Explicit workspace history selection",
              result.time?.archived !== undefined ? "archived" : "history");
          } catch {
            if (before === navigation && historySource() && snapshot === historyRows) {
              byId("search-status").textContent = "Workspace history selection could not be verified. Refresh to retry.";
            }
            return;
          } finally { verifyingHistory = false; }
        } else {
          if (!membershipKnown || !members.includes(item)) return;
          select(item, "Explicit TUI-tab click");
        }
        if (manager()) {
          show(lastChat);
          views.find(button => button.dataset.view === view).focus();
        } else if (panel === "sessions") toggleSessions();
      },
    };
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
    workspace = workspaceLayout();
    notice.textContent = data.selectionNotice;
    render();
    if (selected) select(selected, selected.origin); // Existing src is not reassigned.
    refresh.addEventListener("click", () => { if (ready) workspace.refresh(); });
    frame.addEventListener("load", () => { if (ready) { sawFrameLoad = true; frameReady(); } });
    window.addEventListener("load", () => { if (ready && !sawFrameLoad) frameReady(); }, { once: true });
    const startObserver = () => {
      if (timer === null) timer = window.setInterval(() => { void observeNativeRoute(); }, 1000);
    };
    startObserver();
    window.addEventListener("pagehide", () => {
      window.clearInterval(timer);
      timer = null;
      workspace.cancel();
    });
    window.addEventListener("pageshow", () => { if (ready) { startObserver(); void observeNativeRoute(); } });
    // Keep the server-rendered links available unless the shell finishes starting.
    document.getElementById("fallback-links").hidden = true;
    window.ocBoxWebShell = Object.freeze({
      version: 1, frame, controls: document.getElementById("session-controls"),
      status: document.getElementById("addon-status"), selection, refresh: refreshTabs
    });
    ready = true;
  } catch {
    if (timer !== null) { window.clearInterval(timer); timer = null; }
    // On a narrow viewport the script may already have hidden the library.
    // Fall back to the stacked layout so the verified links are reachable.
    delete root.dataset.workspaceSessionsDrawer;
    delete root.dataset.workspaceInspectorDrawer;
    delete root.dataset.workspaceInspector;
    document.getElementById("workspace-library").hidden = false;
    document.getElementById("workspace-library").inert = false;
    document.getElementById("workspace-overview").hidden = false;
    document.getElementById("workspace-chat").inert = false;
    document.getElementById("fallback-links").hidden = false;
    status.textContent = "The shell could not initialize. Use explicit fallback links or native Home.";
  }
})();
