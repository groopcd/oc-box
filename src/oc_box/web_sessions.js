// Common shell v1. The native app and its composer are opaque: never inspect,
// inject into, record or serialize iframe DOM, browser stores or draft values.
(() => {
  "use strict";
  const root = document.getElementById("web-shell");
  if (!root) return;
  const namedPage = root.dataset.namedStudio === "" || root.dataset.guestStudio === "";
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
  let membershipRefreshFailed = false;
  let workspace = null;
  let ready = false;
  let recentMore = false;
  let named = null;
  let namedCatalogRead = null;
  let namedVerification = null;
  let namedOpenFailed = false;
  const conversationLabel = namedPage ? "Workspace conversation" : "Native conversation";

  function row(value) {
    if (!value || typeof value.id !== "string" || !idPattern.test(value.id) || typeof value.title !== "string" ||
        (namedPage ? Object.hasOwn(value, "route") :
          typeof value.route !== "string" ||
          !/^\/server\/[A-Za-z0-9_-]+\/session\/ses[A-Za-z0-9_-]+$/.test(value.route) ||
          !value.route.endsWith("/session/" + value.id) ||
          (nativePrefix && value.route !== nativePrefix + value.id)) ||
         (value.activeRoot != null && (typeof value.activeRoot !== "string" ||
            !idPattern.test(value.activeRoot)))) throw new Error("catalog");
    return { id: value.id, title: value.title.slice(0, 256), ...(namedPage ? {} : { route: value.route }),
      activeRoot: value.activeRoot ?? null };
  }
  function memberRows(data) {
    if (!Array.isArray(data.sessions) || (namedPage && (
        data.sessions.length > 512 || !["valid", "missing", "unavailable"].includes(data.status) ||
        (data.status !== "valid" && data.sessions.length)))) throw new Error("catalog");
    const next = data.sessions.map(row);
    if (new Set(next.map(item => item.id)).size !== next.length) throw new Error("catalog");
    return next;
  }
  function selection() {
    if (!selected || awaitingLoad || workspace.manager()) return null;
    if (!namedPage) {
      try {
        const url = new URL(frame.contentWindow.location.href);
        if (url.origin !== window.location.origin || url.pathname !== selected.route) return null;
      } catch { return null; }
    }
    return Object.freeze({ sessionID: selected.id, ...(namedPage ? {} : { nativeRoute: selected.route }),
      activeRoot: membershipKnown && members.some(item => item.id === selected.activeRoot) ? selected.activeRoot : null,
      member: selected.member, origin: selected.origin });
  }
  function changed() {
    if (namedPage) return;
    // Attachment add-ons may mount controls in #session-controls and use this
    // exact iframe. No nested shell, second strip or native-store mutation.
    window.dispatchEvent(new CustomEvent("oc-box:selection-changed", {
      detail: selection()
    }));
  }
  function clearNamedSelection(renderNow = true) {
    navigation += 1;
    namedVerification?.abort();
    selected = null;
    namedOpenFailed = false;
    if (renderNow) render();
    try { named.selectionChanged(null); }
    catch { return false; }
    return true;
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
  function sessionButton(item, source) {
    const button = document.createElement("button");
    button.type = "button";
    button.title = item.id;
    button.dataset.sessionId = item.id;
    button.dataset.sessionSource = source;
    button.addEventListener("click", event => {
      if (!namedPage || (event.isTrusted && workspace.source() === source)) void workspace.open(item, event);
    });
    return button;
  }
  function renderRail(rows, activeId, currentId, openIds) {
    const focused = document.activeElement?.dataset;
    const source = workspace.source();
    const isTabs = source === "tabs";
    let refocus;
    for (const [kind, { list, panel }] of Object.entries(groups)) {
      list.replaceChildren();
      panel.hidden = kind !== source;
    }
    for (const item of rows) {
      const kind = isTabs ? "tabs" : item.archived ? "archived" : "history";
      const button = sessionButton(item, source);
      button.className = "workspace-session" + (item.child ? " workspace-child" : "");
      const title = item.title || item.id;
      if (isTabs) {
        button.setAttribute("role", "tab");
        button.setAttribute("aria-controls", namedPage ? "workspace-chat" : "native");
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
      description.textContent = isTabs ? (namedPage ? "Recent workspace session" : "Open TUI tab") :
        [item.child ? "Child session" : null, item.archived ? "Archived" : null,
          openIds.has(item.id) ? "In open TUI tabs" : null]
          .filter(Boolean).join(" · ") || "Workspace history";
      copy.append(titleNode, description);
      button.append(rowIcon(item.child ? "child" : item.archived ? "archive" : "chat"), copy);
      groups[kind].panel.hidden = false;
      groups[kind].list.append(button);
      if (focused?.sessionId === item.id && focused.sessionSource === source) refocus = button;
    }
    refocus?.focus({ preventScroll: true });
  }
  function renderManagerRows(rows, openIds) {
    const tbody = byId("manager-session-rows");
    const source = workspace.source();
    const focused = document.activeElement?.dataset;
    let refocus;
    const rendered = rows.map(item => {
      const tr = document.createElement("tr");
      const title = item.title || item.id;
      const button = sessionButton(item, source);
      button.className = "workspace-manager-text-action";
      button.setAttribute("aria-label", (namedPage ? "Open in Studio: " : "Open in native chat: ") + title);
      button.setAttribute("dir", "auto");
      button.textContent = title;
      if (selected?.id === item.id) {
        tr.className = "is-selected";
        button.setAttribute("aria-current", "true");
      }
      if (focused?.sessionId === item.id && focused.sessionSource === source) refocus = button;
      const session = document.createElement("td");
      const id = document.createElement("small");
      id.textContent = item.id;
      session.append(button, id);
      const provenance = document.createElement("td");
      provenance.textContent = source === "tabs" ? (namedPage ? "Recent workspace session" : "Open TUI tab") :
        (item.archived ? "Archived in workspace" : "Workspace history") + (item.child ? " · Child session" : "");
      const membership = document.createElement("td");
      membership.textContent = namedPage ? "Not reported" : !membershipKnown ? "Unknown" :
        openIds.has(item.id) ? "Open in TUI" : "Not in current TUI catalog";
      tr.append(session, provenance, membership);
      return tr;
    });
    tbody.replaceChildren(...rendered);
    byId("manager-session-table").hidden = rendered.length === 0;
    refocus?.focus({ preventScroll: true });
  }
  function render() {
    const current = awaitingLoad ? selected : selection();
    const currentId = awaitingLoad ? selected?.id : current?.sessionID;
    const openIds = new Set(membershipKnown ? members.map(item => item.id) : []);
    const activeId = openIds.has(current?.activeRoot) ? current.activeRoot : currentId;
    if (!workspace.manager()) renderRail(workspace.visibleRows(), activeId, currentId, openIds);
    else if (workspace.managerSessions()) renderManagerRows(workspace.visibleRows(), openIds);
    retained.replaceChildren();
    conversationTitle.textContent = selected?.title || conversationLabel;
    conversationSource.textContent = "OpenCode / " + conversationLabel;
    if (namedPage) empty.firstElementChild.textContent = namedVerification && !namedVerification.signal.aborted && !selected
      ? "Verifying the selected session…" : selected
        ? "Studio transcript is not displayed. Choose the session again or use the host terminal."
        : namedOpenFailed ? "Session selection could not be verified. Choose a session to try again."
          : "Choose a verified session from this workspace.";
    currentSession.textContent = selected ? (selected.title || selected.id) + " · " + selected.id :
      "No conversation selected.";
    if (selected) {
      selected.member = openIds.has(selected.id);
      const childOfOpenTab = !selected.member && openIds.has(selected.activeRoot);
      const sourceLabel = selected.historyKind === "archived" ? "Archived in workspace" :
        selected.historyKind === "history" ? "Workspace history" :
        selected.member ? "Open TUI tab" : childOfOpenTab ? "Child of open TUI tab" : "Browser selection";
      conversationSource.textContent = "OpenCode / " + (namedPage ? "Workspace session" : sourceLabel);
      notice.textContent = namedPage ? selected.origin : selected.origin + (selected.member ? "" : childOfOpenTab ?
        " — viewing a child of open TUI tab " + selected.activeRoot :
        membershipKnown ? " — selection is not in the latest TUI membership; conversation retained" :
          " — TUI membership is unavailable; conversation retained");
      if (!selected.member && !namedPage) {
        const label = document.createElement("p");
        label.textContent = (childOfOpenTab ? "Child conversation in the highlighted TUI tab: " :
          "Retained browser selection (not counted as TUI-open): ") + selected.title + " · " + selected.id;
        retained.append(label);
      }
    } else notice.textContent = namedPage
      ? "No workspace conversation is currently selected."
      : "No native conversation is currently verified. Choose a tab or use native Home.";
    status.textContent = membershipRefreshFailed ?
      (namedPage ? "TUI membership could not be refreshed." :
        "TUI membership could not be refreshed. Conversation retained; retry or open native Home.") :
      (membershipKnown ? members.length + " verified open TUI tab(s). " :
      "TUI membership unavailable; open count unknown. ") +
      "Refresh does not reload the selected conversation or verify native focus.";
    if (namedPage) status.textContent = membershipRefreshFailed ? "Recent sessions could not be refreshed." :
      membershipKnown ? `${members.length} recent session(s), up to 16.${recentMore ? " More sessions may exist." : ""}` :
      "Recent workspace sessions unavailable.";
    byId("manager-tab-count").textContent = membershipKnown ? String(openIds.size) : "Unknown";
    byId("manager-selection").textContent = selected?.title || selected?.id || "None";
    byId("manager-selection-details").textContent = selected ?
      (selected.title || selected.id) + " · " + selected.id + ". " + notice.textContent : "No browser conversation selected.";
    byId("manager-membership-status").textContent = namedPage ? status.textContent :
      membershipRefreshFailed ? "Refresh failed; open count unknown." :
      membershipKnown ? `${openIds.size} verified open TUI tabs` : "Unavailable; open count unknown.";
    byId("manager-history-status").textContent = workspace.historySummary();
    workspace.updateRefresh();
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
    workspace.updateRefresh();
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
      workspace.updateRefresh();
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
    if (busy || (namedPage && !named.readCatalog)) return;
    busy = true;
    workspace.updateRefresh();
    const controller = namedPage ? new AbortController() : null;
    namedCatalogRead = controller;
    try {
      let data;
      if (namedPage) {
        data = await named.readCatalog({ signal: controller.signal });
        if (controller.signal.aborted) return;
        recentMore = data.hasEarlier === true;
      } else {
        const response = await fetch(endpoint, { credentials: "same-origin", headers: { Accept: "application/json" } });
        if (!response.ok) throw new Error("catalog");
        data = await response.json();
      }
      members = memberRows(data);
      membershipKnown = data.status === "valid";
      membershipRefreshFailed = false;
      render(); // Deliberately never select/reload a frame on metadata refresh.
      changed();
    } catch {
      if (controller?.signal.aborted) return;
      membershipKnown = false;
      membershipRefreshFailed = true;
      recentMore = false;
      members = []; // A failed refresh cannot leave a stale count or action.
      render();
      changed();
    } finally {
      namedCatalogRead = null;
      busy = false;
      workspace.updateRefresh();
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
    const managerRegion = byId("manager");
    const sections = buttons("manager-nav").map(tab => ({
      tab, name: tab.dataset.section, panel: document.getElementById(tab.getAttribute("aria-controls")),
    }));
    const statusRefresh = byId("manager-status-refresh"), sessionRefresh = byId("manager-session-refresh");
    const managerSource = byId("manager-source"), managerSearch = byId("manager-search");
    const includeArchives = byId("manager-include-archives"), clearSearch = byId("manager-clear-search");
    const density = byId("manager-density"), expandDetails = byId("manager-expand-details");
    const displayDefaults = { density: "comfortable", expanded: false };
    let display = displayDefaults;
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
      manager: ["Workspace manager.", "Seven views without chat. This browser can access only workspace metadata."],
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
    const managerSessions = () => manager() && section === "sessions";
    const sessionsDrawer = () => !manager() && (view === "focus" || !roomy.matches);
    const inspectorDrawer = () => view === "focus" || (view === "studio" && !wide.matches);
    const historySource = () => source !== "tabs";
    const listStatus = () => byId(managerSessions() ? "manager-session-count" : "search-status");
    const refreshSource = () => historySource() ? loadHistory() : refreshTabs();
    function updateRefresh() {
      statusRefresh.disabled = busy || (namedPage && !named.readCatalog);
      refresh.disabled = sessionRefresh.disabled = historySource()
        ? historyState === "loading" || (namedPage && !named.readHistory)
        : busy || (namedPage && !named.readCatalog);
    }

    async function loadHistory() {
      historyController?.abort();
      const controller = new AbortController();
      historyController = controller;
      historyState = "loading";
      historyRows = [];
      render();
      try {
        let values;
        if (namedPage) {
          if (!named.readHistory) throw new Error("history");
          values = await named.readHistory({ signal: controller.signal });
        } else {
          const response = await fetch(historyEndpoint, {
            credentials: "same-origin", headers: { Accept: "application/json" },
            signal: controller.signal,
          });
          if (!response.ok) throw new Error("history");
          values = await response.json();
        }
        if (!Array.isArray(values) || values.length > 1000) throw new Error("history");
        const received = values.map(value => {
          if (!value || typeof value.id !== "string" || !idPattern.test(value.id) ||
              typeof value.title !== "string" || typeof value.isChild !== "boolean" ||
              !value.time || typeof value.time !== "object") throw new Error("history");
          return { ...row(namedPage ? value : { id:value.id, title:value.title, route:nativePrefix + value.id }),
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
          render();
        }
      }
    }
    function setSource(value) {
      if (!Object.hasOwn(sources, value)) return;
      if (source !== value) {
        navigation += 1; // An away/back source change invalidates pending opens.
        namedVerification?.abort();
      }
      source = value;
      for (const [kind, button] of Object.entries(sources)) {
        button.setAttribute("aria-pressed", String(value === kind));
      }
      managerSource.value = source;
      byId("manager-archive-filter").hidden = source !== "history";
      const label = historySource() ? "Refresh workspace history" :
        namedPage ? "Refresh recent sessions" : "Refresh TUI tabs";
      for (const button of [refresh, sessionRefresh]) {
        button.setAttribute("aria-label", label);
        button.title = label;
      }
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
      for (const { tab, name: panelName, panel: sectionPanel } of sections) {
        const active = panelName === section;
        tab.setAttribute("aria-selected", String(active));
        tab.tabIndex = active ? 0 : -1;
        sectionPanel.hidden = !active;
      }
      managerRegion.hidden = !manager();
      statusRefresh.hidden = section !== "status";
      byId("content").hidden = manager();
      chat.hidden = manager(); // Never move the frame or write its src here.
      chat.inert = panel !== null;
      overview.hidden = manager() || (inspectorDrawer() ? panel !== "inspector" : view === "studio" && !dockedInspector);
      library.hidden = manager() || (sessionsDrawer() && panel !== "sessions");
      library.inert = panel === "inspector";
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
      byId("inspector-context").textContent = view === "dashboard" ?
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
      namedVerification?.abort();
      render();
      changed(); // Invalidate File reads even for a round trip to the same selection.
      if (namedPage && manager()) clearNamedSelection();
      if (manager()) sections.find(entry => entry.name === section).tab.focus();
      else if (!namedPage) void observeNativeRoute();
    }
    function showSection(next, focus = false) {
      if (!manager()) return;
      if (section !== next.name) {
        navigation += 1;
        section = next.name;
        apply();
        render();
      }
      if (focus) next.tab.focus();
    }
    function stageDisplay(value, message) {
      density.value = value.density;
      expandDetails.checked = value.expanded;
      byId("settings-notice").textContent = message;
    }
    function applyDisplay() {
      if (density.value !== "comfortable" && density.value !== "compact") return;
      display = { density: density.value, expanded: expandDetails.checked };
      if (display.density === "compact") managerRegion.dataset.compact = "";
      else delete managerRegion.dataset.compact;
      byId("manager-details").open = display.expanded;
      byId("settings-notice").textContent = "Applied for this page only. No host changes.";
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
    sections.forEach((entry, index) => {
      on(entry.tab, "click", () => showSection(entry, true));
      on(entry.tab, "keydown", event => {
        if (event.defaultPrevented || event.isComposing || event.altKey || event.ctrlKey || event.metaKey) return;
        const last = sections.length - 1;
        const target = { ArrowLeft: index ? index - 1 : last,
          ArrowRight: index === last ? 0 : index + 1, Home: 0, End: last };
        const next = Object.hasOwn(target, event.key) ? target[event.key] :
          /^[1-7]$/.test(event.key) ? Number(event.key) - 1 : null;
        if (next === null) return;
        event.preventDefault();
        showSection(sections[next], true);
      });
    });
    on(statusRefresh, "click", () => { if (manager() && section === "status") void refreshTabs(); });
    on(sessionRefresh, "click", () => { if (managerSessions()) void refreshSource(); });
    on(managerSource, "change", () => { if (managerSessions()) setSource(managerSource.value); });
    on(managerSearch, "input", render);
    on(includeArchives, "change", () => {
      navigation += 1;
      render();
    });
    on(clearSearch, "click", () => {
      managerSearch.value = "";
      render();
      managerSearch.focus();
    });
    on(byId("settings-apply"), "click", applyDisplay);
    on(byId("settings-defaults"), "click", () => stageDisplay(displayDefaults, "Defaults staged. Apply to use them."));
    on(byId("settings-revert"), "click", () => stageDisplay(display, "Draft reverted."));
    on(managerRegion, "keydown", event => {
      if (section !== "settings" || event.key !== "Escape" || event.defaultPrevented || event.isComposing) return;
      event.preventDefault();
      stageDisplay(display, "Draft reverted.");
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
      managerSessions,
      source: () => source,
      updateRefresh,
      historySummary: () => historyState === "valid" ? `${historyRows.length} verified project records` :
        { idle: "Not requested", loading: "Loading…", unavailable: "Unavailable" }[historyState],
      refresh: refreshSource,
      cancel() {
        if (historyController) {
          historyController.abort();
          historyController = null;
          historyState = "idle";
          render();
        }
      },
      visibleRows() {
        const field = managerSessions() ? managerSearch : search;
        const query = field.value.slice(0, 128).trim().toLowerCase();
        const history = historySource();
        const excludeArchives = managerSessions() && source === "history" && !includeArchives.checked;
        const list = source === "archived" ? historyRows.filter(item => item.archived) :
          excludeArchives ? historyRows.filter(item => !item.archived) :
          history ? historyRows : membershipKnown ? members : [];
        clearSearch.disabled = !managerSearch.value;
        const visible = list.filter(item =>
          !query || item.title.toLowerCase().includes(query) || item.id.toLowerCase().includes(query));
        listStatus().textContent = history ?
          historyState === "loading" ? "Loading workspace history…" :
          historyState !== "valid" ? (namedPage && !named.readHistory
            ? "Workspace history is not available in this browser."
            : "Workspace history unavailable. Refresh to retry.") :
          list.length === 0 ? (source === "archived" ? "No archived sessions in this workspace project." :
            excludeArchives ? "No non-archived sessions in this workspace project." : "No sessions in this workspace project.") :
          visible.length === 0 ? "No workspace history matches this search." :
          `${visible.length} of ${list.length} ${source === "archived" ? "archived" : "workspace history"} rows. Not TUI membership.` :
          namedPage ? (!membershipKnown ? "Recent workspace sessions unavailable." :
            members.length === 0 ? "No recent workspace sessions." :
            visible.length === 0 ? "No recent sessions match this search." :
            `${visible.length} of ${members.length} recent sessions.`) :
          !membershipKnown ? "Verified open TUI sessions unavailable." :
          members.length === 0 ? "No verified open TUI sessions." :
          visible.length === 0 ? "No open TUI sessions match this search." :
          `${visible.length} of ${members.length} open TUI tabs.`;
        return visible;
      },
      async open(item, event) {
        if (!ready || (manager() && !managerSessions())) return;
        if (namedPage) {
          const history = historySource();
          const snapshot = history ? historyRows : members;
          if (namedVerification || document.hidden || !snapshot.includes(item) ||
              (history ? historyState !== "valid" : !membershipKnown)) return;
          if (!clearNamedSelection(false)) return;
          const verification = new AbortController();
          let before = navigation;
          try {
            // The controller must consume this trusted click before render()
            // replaces its attached row or a layout change moves focus.
            const pending = named.verifySelection(item.id, { signal: verification.signal, event });
            if (manager()) {
              show(lastChat);
              views.find(button => button.dataset.view === view).focus();
            } else if (panel === "sessions") toggleSessions();
            // Leaving the manager invalidates the old view. Only bind the
            // new verification to the destination after that transition.
            before = navigation;
            namedVerification = verification;
            render();
            const verified = await pending;
            if (verification.signal.aborted || before !== navigation || document.hidden || manager() ||
                snapshot !== (history ? historyRows : members)) return;
            if (verified !== true) { namedOpenFailed = true; return; }
            selected = {
              ...item,
              origin: history ? "Explicit workspace history selection" : "Explicit recent workspace selection",
              historyKind: history ? (item.archived ? "archived" : "history") : null,
              activeRoot: item.activeRoot ?? (members.some(value => value.id === item.id) ? item.id : null),
            };
            render();
            named.selectionChanged(selection());
          } catch {
            selected = null;
            namedOpenFailed = !verification.signal.aborted && before === navigation;
          } finally {
            namedVerification = null;
            // A refused or superseded verifier may still have resolved a scope.
            if (!selected) {
              try { named.selectionChanged(null); } catch { /* No display or authority. */ }
            }
            render();
          }
          return;
        }
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
              listStatus().textContent = "Workspace history selection could not be verified. Refresh to retry.";
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
  function initialize(adapter) {
    const data = JSON.parse(root.dataset.bootstrap);
    if (namedPage) {
      if (!root.isConnected || root.querySelector("iframe") || Object.hasOwn(data, "nativePrefix") ||
          data.selection !== null || typeof adapter?.verifySelection !== "function" ||
          typeof adapter.selectionChanged !== "function" ||
          (adapter.readCatalog !== undefined && typeof adapter.readCatalog !== "function") ||
          (adapter.readHistory !== undefined && typeof adapter.readHistory !== "function")) return null;
      named = Object.freeze({
        verifySelection: adapter.verifySelection.bind(adapter),
        selectionChanged: adapter.selectionChanged.bind(adapter),
        readCatalog: adapter.readCatalog?.bind(adapter),
        readHistory: adapter.readHistory?.bind(adapter),
      });
    } else {
      if (typeof data.nativePrefix !== "string" ||
          !/^\/server\/[A-Za-z0-9_-]+\/session\/$/.test(data.nativePrefix)) throw new Error("prefix");
      nativePrefix = data.nativePrefix;
    }
    membershipKnown = data.status === "valid";
    members = memberRows(data);
    if (data.selection) {
      selected = { ...row(data.selection), origin: data.selection.origin,
        member: members.some(item => item.id === data.selection.id) };
    }
    workspace = workspaceLayout();
    notice.textContent = data.selectionNotice;
    render();
    if (!namedPage && selected) select(selected, selected.origin); // Existing src is not reassigned.
    refresh.addEventListener("click", () => { if (ready) workspace.refresh(); });
    if (namedPage) {
      function suspend() {
        namedCatalogRead?.abort();
        workspace.cancel();
        clearNamedSelection();
      }
      window.addEventListener("pagehide", suspend);
      document.addEventListener("visibilitychange", () => { if (document.hidden) suspend(); });
      window.ocBoxNamedStudioChrome = Object.freeze({ version: 1, selection, refresh: refreshTabs });
      ready = true;
      return window.ocBoxNamedStudioChrome;
    }
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
  }
  function mount(adapter) {
    try { return initialize(adapter); }
    catch {
      if (timer !== null) { window.clearInterval(timer); timer = null; }
      // On a narrow viewport the script may already have hidden the library.
      // Fall back to the stacked layout so the verified links are reachable.
      delete root.dataset.workspaceSessionsDrawer;
      delete root.dataset.workspaceInspectorDrawer;
      delete root.dataset.workspaceInspector;
      byId("library").hidden = false;
      byId("library").inert = false;
      byId("overview").hidden = false;
      byId("chat").inert = false;
      if (namedPage) empty.firstElementChild.textContent = "Studio controls could not initialize. Use the host terminal.";
      else {
        document.getElementById("fallback-links").hidden = false;
        status.textContent = "The shell could not initialize. Use explicit fallback links or native Home.";
      }
      return null;
    }
  }
  if (namedPage) {
    // The separately reviewed named entry supplies these callbacks, not a
    // URL or ambient authority. verifySelection(id, {signal, event}) is
    // invoked synchronously in the actual click and resolves true
    // only for that exact, currently admitted scope. selectionChanged(null)
    // synchronously clears the view and revokes/cancels the local controller
    // scope; non-null is a display cross-check after verification, not a grant.
    // Optional readCatalog({signal}) returns {status, sessions:[{id, title}]};
    // readHistory({signal}) returns workspace-only {id, title, isChild, time}
    // rows. Neither callback is ever invoked to guess or grant selection.
    window.ocBoxMountNamedStudioChrome = adapter => !named ? mount(adapter) : null;
  } else {
    mount();
  }
})();
