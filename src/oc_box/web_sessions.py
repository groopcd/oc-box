"""Authenticated open-TUI-session index, not native browser tab manipulation.

Native 2.2.6/2.3.0 web tabs are client-local (app shell/tabs/tabs.tsx).
The public /server/:serverKey/session/:id route opens one session, not a tab
inventory. Keep that app unchanged and offer explicit navigation here instead.
"""

import base64
from html import escape
import json
import os
from pathlib import Path
import re
import select
import subprocess
import time
from urllib.parse import parse_qs, urlsplit

if __package__:
    from . import native_tabs, opencode_v2_io
else:
    import native_tabs
    import opencode_v2_io

# Native 2.2.6/2.3.0 app/vite.pwa.ts excludes /auth/ from its navigation
# fallback. A path outside that namespace can be swallowed by an installed SW.
INDEX = "/auth/oc-box-sessions"
SCRIPT = INDEX + ".js"
STYLE = INDEX + ".css"
CATALOG = INDEX + "/catalog"
SELECTION = INDEX + "/selection"
HISTORY = INDEX + "/history"
NATIVE_HOME = '<a href="/" target="_blank" rel="noopener noreferrer">Open native OpenCode Home</a>'


def native_session_route(url, ident):
    encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return "/server/" + encoded + "/session/" + ident


def open_sessions(box, data, connection):
    """List verified native membership, not automatic-resume/history candidates.

    An idle tab remains open. Activity is deliberately not consulted. The
    native saved scope may contain worktree/moved, archived or child IDs.
    Every listed ID must still exist in THIS private service; no other service
    or host history is queried and no missing membership is guessed.
    """
    if connection.get("workspace") != data["id"]:
        raise box.BoxError("Open web sessions could not be verified.")
    box.workspace_service.validate_endpoint(connection)
    snapshot = native_tabs.inspect(box, data, connection=connection)
    if snapshot.status not in ("valid", "missing"):
        raise box.BoxError("Open web sessions could not be verified.")
    if snapshot.status == "missing" or not snapshot.tabs:
        return [], snapshot.status
    inventory = box.workspace_service.web_session_catalog(
        box, data, connection, snapshot.tabs
    )
    if native_tabs.inspect(box, data, connection=connection) != snapshot:
        raise box.BoxError("Open web sessions changed; refresh the session index.")
    if not isinstance(inventory, list):
        raise box.BoxError("Open web sessions could not be verified.")
    verified = {}
    for info in inventory:
        opencode_v2_io.web_session_info(info)
        if info["id"] in verified:
            raise box.BoxError("Open web sessions could not be verified.")
        verified[info["id"]] = info
    return [
        verified[ident] for ident in snapshot.tabs if ident in verified
    ], snapshot.status


def home_navigation():
    """Explicit native UI navigation, never an inferred TUI/session target."""
    return (
        "<p>" + NATIVE_HOME + " to explicitly create or open a browser "
        "session. Browser-only tabs remain native-client local; they do not "
        "populate this TUI list.</p>"
    )


def explicit_session(box, data, connection, ident, *, timeout=None):
    """Explicit selection only; existing identity in this private DB, not history guessing."""
    if not box.valid_session_id(ident):
        raise box.BoxError("Invalid explicit web session ID.")
    found = box.workspace_service.web_session_catalog(
        box,
        data,
        connection,
        [ident],
        **({"timeout": timeout} if timeout is not None else {}),
    )
    if len(found) != 1 or opencode_v2_io.web_session_info(found[0])["id"] != ident:
        raise box.BoxError(
            "Explicit web session is unavailable in this private service."
        )
    return found[0]


def selection_report(gateway):
    """Adapt the verified native reader without treating IDs as authorization."""
    reader = getattr(gateway, "selection_snapshot_reader", None)
    if reader is None:
        return None
    try:
        value = reader(gateway.box, gateway.data, gateway.connection)
        if isinstance(value, dict):
            # Retain the original trusted normalized-reader extension contract.
            return {
                key: value.get(key) for key in ("status", "selectedSessionID", "source")
            }
        if getattr(value, "status", None) != "valid":
            return {"status": "unavailable"}
        tabs = value.tabs
        if (
            not isinstance(tabs, tuple)
            or len(tabs) > native_tabs.MAX_TABS
            or any(not gateway.box.valid_session_id(ident) for ident in tabs)
            or len(set(tabs)) != len(tabs)
            or (
                value.selected is not None
                and not gateway.box.valid_session_id(value.selected)
            )
            or (value.activeRoot is not None and value.activeRoot not in tabs)
            or value.source not in ("owner-tui", "single-tui")
        ):
            return {"status": "unavailable"}
        return {
            "status": "verified",
            "selectedSessionID": value.selected,
            "activeRoot": value.activeRoot,
            "tabs": tabs,
            "source": value.source,
            "_native_snapshot": True,
        }
    except Exception:
        # Never render private RPC/native errors or fall back to stale focus.
        return {"status": "unavailable"}


def reported_open_sessions(gateway, report):
    """Fresh verified reports supply membership as well as exact selection."""
    if not report or not report.get("_native_snapshot"):
        return open_sessions(gateway.box, gateway.data, gateway.connection)
    ids = report["tabs"]
    if not ids:
        return [], "valid"
    found = gateway.box.workspace_service.web_session_catalog(
        gateway.box, gateway.data, gateway.connection, ids
    )
    verified = {}
    for info in found:
        opencode_v2_io.web_session_info(info)
        if info["id"] in verified:
            raise gateway.box.BoxError("Open web sessions could not be verified.")
        verified[info["id"]] = info
    if set(verified) != set(ids):
        raise gateway.box.BoxError(
            "Open web sessions changed; refresh the session index."
        )
    return [verified[ident] for ident in ids], "valid"


def root_member(gateway, info, members):
    """Resolve native ancestry for display only, never replace the exact route."""
    if not members:
        return None
    seen = set()
    deadline = time.monotonic() + 4
    for _ in range(32):
        ident = info["id"]
        if ident in seen:
            return None
        seen.add(ident)
        if ident in members:
            return ident
        parent = info.get("parentID")
        if parent is None or not gateway.box.valid_session_id(parent):
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            info = explicit_session(
                gateway.box, gateway.data, gateway.connection, parent, timeout=remaining
            )
        except (
            gateway.box.BoxError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            subprocess.SubprocessError,
        ):
            # Relationship display is optional; an explicit existing child
            # remains usable even when its parent cannot be verified.
            return None
    return None


def resolve_initial_selection(
    members,
    *,
    requested=None,
    override=None,
    declared=None,
    report=None,
    report_requested=False,
):
    """Explicit selection wins; only a verified native snapshot supplies TUI focus."""
    if override is not None:
        return override, "Explicit --session selection; not verified native focus"
    if requested is not None:
        return requested, "URL-selected conversation; not verified native focus"
    if report_requested:
        # Native snapshots already verify exact child/root identity through this
        # private service. Legacy normalized readers admit direct members only.
        if (
            isinstance(report, dict)
            and report.get("status") == "verified"
            and isinstance(report.get("selectedSessionID"), str)
            and (
                report.get("selectedSessionID") in members
                or report.get("_native_snapshot") is True
            )
            and isinstance(report.get("source"), str)
        ):
            return report[
                "selectedSessionID"
            ], "Verified TUI selection report: " + report["source"][:128]
        return None, "Native TUI selection report unavailable; choose a tab"
    if declared in members:
        return declared, "Launcher-declared selection; not verified native focus"
    return None, "Choose a tab; native TUI focus is not verified"


def catalog(url, sessions, status):
    return {
        "status": status,
        "nativePrefix": native_session_route(url, "ses_placeholder").rsplit("/", 1)[0]
        + "/",
        "sessions": [
            {
                "id": info["id"],
                "title": (info.get("title") or info["id"])[:256]
                if isinstance(info.get("title"), str)
                else info["id"],
                "route": native_session_route(url, info["id"]),
            }
            for info in sessions
        ],
    }


_UNREAD_REPORT = object()


def initial_state(gateway, sessions, status, requested=None, *, report=_UNREAD_REPORT):
    state = catalog(gateway.url, sessions, status)
    members = {info["id"]: info for info in state["sessions"]}
    reader = getattr(gateway, "selection_snapshot_reader", None)
    override = gateway.connection.get("web_session_override")
    if report is _UNREAD_REPORT:
        report = (
            selection_report(gateway)
            if reader is not None and override is None and requested is None
            else None
        )
    ident, origin = resolve_initial_selection(
        members,
        requested=requested,
        override=override,
        declared=gateway.connection.get("session"),
        report=report,
        report_requested=reader is not None,
    )
    selected = None
    if ident is not None:
        selected = members.get(ident)
        if selected is None:
            info = explicit_session(
                gateway.box, gateway.data, gateway.connection, ident
            )
            selected = catalog(gateway.url, [info], status)["sessions"][0]
        selected = {**selected, "origin": origin, "member": ident in members}
        if ident in members:
            selected["activeRoot"] = ident
        elif (
            report
            and report.get("_native_snapshot")
            and report.get("selectedSessionID") == ident
            and report.get("activeRoot") in members
        ):
            selected["activeRoot"] = report["activeRoot"]
        else:
            selected["activeRoot"] = root_member(gateway, info, members)
    return {**state, "selection": selected, "selectionNotice": origin}


_ICONS = """<svg class="workspace-icons" width="0" height="0" aria-hidden="true">
  <symbol id="i-box" viewBox="0 0 24 24"><path d="m12 2 9 5v10l-9 5-9-5V7zM3 7l9 5 9-5m-9 5v10m-4.5-17.5 9 5"/></symbol>
  <symbol id="i-user" viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21v-2a5 5 0 0 1 5-5h6a5 5 0 0 1 5 5v2"/></symbol>
  <symbol id="i-search" viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="6"/><path d="m15 15 5 5"/></symbol>
  <symbol id="i-grid" viewBox="0 0 24 24"><path d="M3 4h18v16H3zm7 0v16m0-10h11"/></symbol>
  <symbol id="i-chat" viewBox="0 0 24 24"><path d="M4 4h16v13H9l-5 4zM8 9h8m-8 4h5"/></symbol>
  <symbol id="i-archive" viewBox="0 0 24 24"><path d="M3 4h18v5H3zm2 5v11h14V9m-9 4h4"/></symbol>
  <symbol id="i-child" viewBox="0 0 24 24"><path d="M5 4v12h14m-4-4 4 4-4 4"/></symbol>
</svg>"""


def _icon(name):
    return (
        f'<svg class="workspace-icon" aria-hidden="true"><use href="#i-{name}"/></svg>'
    )


def _workspace_name(data):
    # Display only the leaf of the registered in-container directory. In
    # particular, do not reveal a restored workspace's host destination name.
    path = data.get("container_project", data.get("project"))
    if not isinstance(path, str) or not path.startswith("/"):
        return "Private workspace"
    leaf = path.rstrip("/").rsplit("/", 1)[-1]
    label = "".join(char for char in leaf[:80] if char.isprintable()).strip()
    return label if label and label not in (".", "..") else "Private workspace"


_MANAGER_VIEWS = (
    ("status", "Status"),
    ("settings", "Settings"),
    ("sessions", "Sessions"),
    ("terminals", "Terminals"),
    ("logs", "Logs"),
    ("storage", "Storage"),
    ("backups", "Backup/restore"),
)


def _manager_panel(ident, heading, description, body, tag=""):
    hidden = "" if ident == "status" else " hidden"
    badge = f'<span class="workspace-tag">{tag}</span>' if tag else ""
    return f"""<section role="tabpanel" id="workspace-manager-{ident}" aria-labelledby="workspace-tab-{ident}" tabindex="0"{hidden}>
  <div class="workspace-manager-view-head">
    <div><h2>{heading}</h2><p>{description}</p></div>{badge}
  </div>
  {body}
</section>"""


def _workspace_manager(name):
    """The approved no-chat layout with only this browser's existing scope."""
    tabs = "\n".join(
        f'<button type="button" role="tab" id="workspace-tab-{ident}" data-section="{ident}" '
        f'aria-controls="workspace-manager-{ident}" aria-selected="{str(index == 1).lower()}" '
        f'tabindex="{0 if index == 1 else -1}"><b aria-hidden="true">{index}</b> {label}</button>'
        for index, (ident, label) in enumerate(_MANAGER_VIEWS, 1)
    )
    overview = _manager_panel(
        "status",
        "Overview",
        "Verified session metadata for this private workspace. No host resource or activity readings.",
        f"""<div class="workspace-manager-grid">
  <section class="workspace-manager-card"><h3 class="workspace-eyebrow">Workspace</h3>
    <p class="workspace-manager-value"><bdi>{name}</bdi></p><small>Current private service</small></section>
  <section class="workspace-manager-card"><h3 class="workspace-eyebrow">Open TUI tabs</h3>
    <p class="workspace-manager-value" id="workspace-manager-tab-count">Unknown</p><small>Separate from history</small></section>
  <section class="workspace-manager-card"><h3 class="workspace-eyebrow">Browser selection</h3>
    <p class="workspace-manager-value" id="workspace-manager-selection" dir="auto">None</p><small>Not evidence of TUI focus</small></section>
</div>
<div class="workspace-manager-split">
  <section class="workspace-manager-card"><h3>Session sources</h3>
    <div class="workspace-manager-table-scroll"><table class="workspace-manager-table">
      <thead><tr><th scope="col">Source</th><th scope="col">Catalog status</th></tr></thead>
      <tbody><tr><td>Open TUI tabs</td><td><span id="workspace-manager-membership-status" role="status">Unavailable</span></td></tr>
        <tr><td>Workspace history</td><td id="workspace-manager-history-status">Not requested</td></tr></tbody>
    </table></div>
  </section>
  <section class="workspace-manager-card"><h3>Selected workspace</h3>
    <p><bdi>{name}</bdi></p>
    <details id="workspace-manager-details"><summary>Workspace details</summary>
      <p>History is limited to the registered workspace project. Native browser tabs are independent of TUI membership.</p>
      <p id="workspace-manager-selection-details" dir="auto"></p>
    </details>
  </section>
</div>""",
        "Workspace only",
    )
    settings = _manager_panel(
        "settings",
        "Settings",
        "Display preferences stay on this page and reset on reload. Host and native configuration do not change.",
        """<div class="workspace-manager-grid">
  <section class="workspace-manager-card"><h3>Display</h3><div class="workspace-manager-fields">
    <div class="workspace-manager-field"><label for="workspace-manager-density">Density</label>
      <select id="workspace-manager-density">
      <option value="comfortable">Comfortable</option><option value="compact">Compact</option>
    </select></div>
    <label class="workspace-manager-check"><input type="checkbox" id="workspace-manager-expand-details"> Expand workspace details</label>
  </div></section>
  <section class="workspace-manager-card"><h3>Host settings</h3>
    <p>Host settings, resource controls and automatic monitoring are not available in this browser.</p>
  </section>
</div>
<div class="workspace-manager-toolbar">
  <button type="button" class="workspace-action" id="workspace-settings-apply">Apply</button>
  <button type="button" class="workspace-action" id="workspace-settings-defaults">Reset to defaults</button>
  <button type="button" class="workspace-action" id="workspace-settings-revert">Revert</button>
  <span class="workspace-tag">This page only</span>
  <p id="workspace-settings-notice" role="status"></p>
</div>""",
    )
    sessions = _manager_panel(
        "sessions",
        "Sessions",
        "Browse this workspace’s session metadata. Opening a row returns to native chat without adding a TUI tab.",
        f"""<div class="workspace-manager-card">
  <div class="workspace-manager-toolbar">
    <div class="workspace-manager-field workspace-manager-source"><label for="workspace-manager-source">Source</label>
      <select id="workspace-manager-source">
        <option value="tabs">Open TUI tabs</option><option value="history">Workspace history</option>
        <option value="archived">Archived in workspace</option>
      </select>
    </div>
    <button type="button" class="workspace-action" id="workspace-manager-session-refresh"
      aria-label="Refresh TUI tabs" title="Refresh TUI tabs">Refresh</button>
  </div>
  <div class="workspace-manager-session-search">
    <label class="workspace-search" for="workspace-manager-search">{_icon("search")}
      <input type="search" id="workspace-manager-search" aria-label="Search sessions"
        placeholder="Search title or session ID…" maxlength="128" autocomplete="off">
    </label>
    <button type="button" class="workspace-action" id="workspace-manager-clear-search" disabled>Clear search</button>
    <label class="workspace-manager-check" id="workspace-manager-archive-filter" hidden>
      <input type="checkbox" id="workspace-manager-include-archives" checked> Include archived
    </label>
    <span class="workspace-tag" id="workspace-manager-session-count" role="status"></span>
  </div>
  <div class="workspace-manager-table-scroll workspace-manager-session-table" id="workspace-manager-session-table" hidden>
    <table class="workspace-manager-table" aria-label="Workspace session results">
      <thead><tr><th scope="col">Session</th><th scope="col">Source</th><th scope="col">TUI membership</th></tr></thead>
      <tbody id="workspace-manager-session-rows"></tbody>
    </table>
  </div>
</div>""",
        "Workspace metadata",
    )
    unavailable = "".join(
        _manager_panel(
            ident,
            heading,
            description,
            f'<section class="workspace-manager-card"><h3>{card}</h3><p>{guidance}</p></section>',
            "Host only",
        )
        for ident, heading, description, card, guidance in (
            (
                "terminals",
                "Terminals",
                "This browser manager cannot attach to or control a terminal.",
                "Native session terminal",
                "Use <code>oc-box terminal</code> from the host to attach to an eligible native workspace session.",
            ),
            (
                "logs",
                "Logs",
                "This browser manager does not read host logs, container output or the system journal.",
                "Launcher activity",
                "Use <code>oc-box logs</code> on the host for the private launcher activity journal.",
            ),
            (
                "storage",
                "Storage",
                "This browser manager cannot scan storage or change workspace resources.",
                "Storage inventory",
                "Open Storage in the local <code>oc-box tui</code> manager to review storage and workspace operations.",
            ),
            (
                "backups",
                "Backup and restore",
                "No archive, key or credential material is available to this browser manager.",
                "Create or restore an archive",
                "Use <code>oc-box backup</code> or <code>oc-box restore</code> on the host and review the operation there.",
            ),
        )
    )
    return f"""<section id="workspace-manager" aria-labelledby="workspace-manager-title" hidden>
  <header class="workspace-manager-head">
    <div><p class="workspace-eyebrow">oc-box / Manager</p><h1 id="workspace-manager-title">Workspace manager</h1>
      <p>This browser view is limited to this workspace. Host operations stay local.</p></div>
    <div class="workspace-manager-toolbar">
      <div class="workspace-manager-field"><span>Workspace context</span><strong><bdi>{name}</bdi></strong></div>
      <button type="button" class="workspace-action" id="workspace-manager-status-refresh" aria-label="Refresh TUI membership">Refresh</button>
      <span class="workspace-tag">Workspace only</span>
    </div>
  </header>
  <div id="workspace-manager-nav" class="workspace-manager-tabs" role="tablist" aria-label="Workspace manager sections">
    {tabs}
  </div>
  {overview}{settings}{sessions}{unavailable}
</section>"""


def _workspace_layout_body(state, conversation, fallback, attachments, workspace_name):
    """Port the final Studio chrome; data and native UI stay behind their existing boundaries."""
    name = escape(workspace_name)
    title = escape((state["selection"] or {}).get("title") or "Native conversation")
    addon_hidden = "" if attachments else " hidden"
    return f"""{_ICONS}
<header id="workspace-masthead">
  <div id="workspace-toolbar">
    <h1 class="workspace-brand">{_icon("box")}oc-box</h1>
    <div class="workspace-project"><span class="workspace-eyebrow">Workspace</span><bdi>{name}</bdi></div>
    <span class="workspace-guest">{_icon("user")}Workspace guest</span>
    <div class="workspace-top-end">
      <span class="workspace-tag"><span class="workspace-dot" aria-hidden="true"></span>Private workspace</span>
      {NATIVE_HOME}
    </div>
  </div>
  <div id="workspace-review">
    <p id="workspace-layout-note"><strong id="workspace-layout-name">Studio.</strong>
      <span id="workspace-layout-description">Sessions, chat and workspace context.</span></p>
    <nav id="workspace-layouts" class="workspace-picker" aria-label="Workspace layout">
      <button type="button" data-view="studio" aria-pressed="true"><b aria-hidden="true">A</b> Studio</button>
      <button type="button" data-view="dashboard" aria-pressed="false"><b aria-hidden="true">B</b> Dashboard</button>
      <button type="button" data-view="focus" aria-pressed="false"><b aria-hidden="true">C</b> Chat focus</button>
      <button type="button" data-view="manager" aria-pressed="false">{_icon("grid")} Workspace manager</button>
    </nav>
  </div>
</header>
<div id="workspace-content">
  <aside id="workspace-library" aria-labelledby="workspace-library-title">
    <div class="workspace-rail-head">
      <h2 id="workspace-library-title">Sessions</h2>
      <div class="workspace-actions">
        <button type="button" class="workspace-action" id="refresh-tabs" aria-label="Refresh TUI tabs" title="Refresh TUI tabs">Refresh</button>
        <button type="button" class="workspace-action" id="workspace-sessions-close" aria-label="Close sessions" hidden>×</button>
      </div>
    </div>
    <label class="workspace-search" for="workspace-search">{_icon("search")}
      <input type="search" id="workspace-search" aria-label="Search by title or session ID"
        placeholder="Search sessions" maxlength="128" autocomplete="off">
    </label>
    <div class="workspace-picker workspace-session-sources" role="group" aria-label="Session source">
      <button type="button" id="workspace-open-tabs" aria-pressed="true">TUI tabs</button>
      <button type="button" id="workspace-history-tabs" aria-pressed="false" aria-label="Workspace history">History</button>
      <button type="button" id="workspace-archived-tabs" aria-pressed="false" aria-label="Archived in workspace">Archived</button>
    </div>
    <section class="workspace-group" id="workspace-open-group">
      <h3 class="workspace-eyebrow" id="workspace-open-title">Open TUI tabs</h3>
      <div id="session-tabs" role="tablist" aria-labelledby="workspace-open-title"></div>
    </section>
    <section class="workspace-group" id="workspace-history-group" hidden>
      <h3 class="workspace-eyebrow">Workspace history</h3><div id="workspace-history-list"></div>
    </section>
    <section class="workspace-group" id="workspace-archived-group" hidden>
      <h3 class="workspace-eyebrow">Archived in workspace</h3><div id="workspace-archived-list"></div>
    </section>
    <p id="workspace-search-status" role="status"></p>
    {fallback}
    <nav class="workspace-rail-nav" aria-label="Workspace context">
      <p class="workspace-eyebrow">Workspace manager</p>
      <button type="button" id="workspace-inspector-link" aria-controls="workspace-overview"
        aria-expanded="true">{_icon("grid")} Status</button>
    </nav>
    <p class="workspace-rail-note">This view shows one workspace, not the host’s sessions or archives.</p>
  </aside>
  <div id="workspace-divider-left" class="workspace-divider" tabindex="0" role="separator" aria-orientation="vertical"
    aria-label="Resize sessions pane" aria-controls="workspace-library"
    title="Drag or use Left/Right; double-click or press Enter to reset" hidden></div>
  <section id="workspace-chat" aria-label="Native conversation">
    <header class="workspace-chat-head">
      <div><p class="workspace-eyebrow" id="workspace-conversation-source">OpenCode / Native conversation</p>
        <h2 id="workspace-conversation-title" dir="auto">{title}</h2></div>
      <div class="workspace-actions">
        <button id="workspace-sessions-toggle" class="workspace-action" type="button"
          aria-expanded="false" aria-controls="workspace-library" hidden>Sessions</button>
        <button id="workspace-inspector-toggle" class="workspace-action" type="button"
          aria-expanded="true" aria-controls="workspace-overview">{_icon("grid")} Inspector</button>
      </div>
    </header>
    <div class="workspace-addons"{addon_hidden}>
      <details><summary>Browser attachments</summary><div id="session-controls"></div></details>
      <p id="addon-status" role="status"{addon_hidden}></p>
    </div>
    {conversation}
  </section>
  <div id="workspace-divider-right" class="workspace-divider" tabindex="0" role="separator" aria-orientation="vertical"
    aria-label="Resize workspace inspector" aria-controls="workspace-overview"
    title="Drag or use Left/Right; double-click or press Enter to reset" hidden></div>
  <aside id="workspace-overview" aria-labelledby="workspace-overview-title">
    <header class="workspace-inspector-head">
      <div><p class="workspace-eyebrow" id="workspace-inspector-context">Workspace inspector</p>
        <h2 id="workspace-overview-title" tabindex="-1">Status</h2></div>
      <button type="button" class="workspace-action" id="workspace-inspector-close" aria-label="Close inspector">×</button>
    </header>
    <div class="workspace-cards">
      <section><h3><bdi>{name}</bdi></h3><p id="catalog-status" role="status"></p></section>
      <section><h3>Browser selection</h3><p id="workspace-current-session" dir="auto">No conversation selected.</p>
        <p id="selection-status">{escape(state["selectionNotice"])}</p><div id="retained-selection"></div></section>
      <section><h3>Session source</h3><p>TUI membership and workspace history are separate. Opening
        history here doesn’t add a TUI tab. Native browser tabs stay independent.</p>
        <p>No host data or workspace health metrics are available.</p>
        <p><a href="{INDEX}">Reload shell</a> (may interrupt a draft).</p></section>
    </div>
  </aside>
</div>
{_workspace_manager(name)}
</main></body></html>"""


def document(
    url,
    sessions,
    status,
    state=None,
    addon_scripts=(),
    *,
    workspace_name="Private workspace",
):
    if (
        not isinstance(addon_scripts, tuple)
        or len(addon_scripts) > 1
        or any(script != "/auth/oc-box-attachments.js" for script in addon_scripts)
    ):
        raise ValueError("Invalid trusted shell addon.")
    state = state or {
        **catalog(url, sessions, status),
        "selection": None,
        "selectionNotice": "Choose a tab; native TUI focus is not verified",
    }
    selected = state["selection"]
    items = []
    for info in sessions:
        ident = info["id"]
        title = info.get("title")
        title = title[:256] if isinstance(title, str) and title else ident
        items.append(
            '<li><a href="'
            + escape(native_session_route(url, ident), quote=True)
            + '" target="_blank" rel="noopener noreferrer">'
            + escape(title)
            + "</a> <small>"
            + escape(ident)
            + "</small></li>"
        )
    conversation = (
        '<section id="no-selection"' + (" hidden" if selected else "") + ">"
        "<p>Select a TUI tab, use --session ID, or open native Home. No conversation is guessed.</p>"
        '</section><iframe id="native" name="oc-box-native-session" '
        'title="Native OpenCode conversation" referrerpolicy="no-referrer"'
        + (
            ' src="' + escape(selected["route"], quote=True) + '"'
            if selected
            else " hidden"
        )
        + "></iframe>"
    )
    fallback = (
        '<details id="fallback-links"><summary>Direct native conversation links (initial verified snapshot)</summary>'
        + (
            "<ol>" + "".join(items) + "</ol>"
            if items
            else "<p>No open session is listed.</p>"
        )
        + "</details>"
    )
    start = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>oc-box — Studio</title><link rel="stylesheet" href="'
        + STYLE
        + '"><script defer src="'
        + SCRIPT
        + '"></script>'
        + "".join(
            '<script defer src="' + script + '"></script>' for script in addon_scripts
        )
        + "</head><body>"
        '<main id="web-shell" data-layout="studio" data-bootstrap="'
        + escape(json.dumps(state, ensure_ascii=True), quote=True)
        + '">'
    )
    return (
        start
        + _workspace_layout_body(
            state, conversation, fallback, bool(addon_scripts), workspace_name
        )
    ).encode("utf-8", errors="replace")


def error_document():
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>oc-box — workspace sessions unavailable</title><body><main>"
        "<h1>Open TUI sessions unavailable</h1><p>Open sessions could not be "
        'verified. <a href="'
        + INDEX
        + '">Refresh session list</a> to retry.</p>'
        + home_navigation()
        + "</main></body></html>"
    ).encode()


def _reply(stream, status, body, content_type="text/plain; charset=utf-8"):
    stream.sendall(
        (
            "HTTP/1.1 "
            + status
            + "\r\nContent-Type: "
            + content_type
            + "\r\nContent-Length: "
            + str(len(body))
            + "\r\nConnection: close\r\nCache-Control: no-store\r\n"
            "Referrer-Policy: no-referrer\r\nX-Content-Type-Options: nosniff\r\n"
            "Content-Security-Policy: default-src 'none'; script-src 'self'; "
            "style-src 'self'; frame-src 'self'; connect-src 'self'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'\r\n\r\n"
        ).encode()
        + body
    )


def _authenticated(child, headers, authority):
    """Ask the SAME native service to validate the caller's native credentials.

    Never substitute the owner's password: loopback alone is not authorization.
    Preserve Host/Origin for native port-scoped cookie and same-origin checks.
    Forward only those headers and the caller's Cookie/Authorization.
    Discard the body and never render, save or log authentication material.
    """
    credentials = []
    seen = set()
    for line in headers.split(b"\r\n")[1:]:
        name, separator, value = line.partition(b":")
        name = name.lower()
        if not separator or name in seen:
            return False
        seen.add(name)
        if name == b"host" and value.strip() != authority.encode():
            return False
        if name in (b"host", b"origin", b"cookie", b"authorization"):
            credentials.append(name + b":" + value)
    if b"host" not in seen:
        return False
    child.stdin.write(
        b"GET /api/info HTTP/1.1\r\nConnection: close\r\n"
        + b"".join(line + b"\r\n" for line in credentials)
        + b"\r\n"
    )
    child.stdin.flush()
    response = bytearray()
    deadline = time.monotonic() + 5
    while b"\r\n\r\n" not in response:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or len(response) >= 65536:
            return False
        if not select.select([child.stdout], [], [], remaining)[0]:
            return False
        chunk = os.read(child.stdout.fileno(), min(4096, 65536 - len(response)))
        if not chunk:
            return False
        response.extend(chunk)
    return response.split(b"\r\n", 1)[0].split(b" ")[:2] in (
        [b"HTTP/1.1", b"200"],
        [b"HTTP/1.0", b"200"],
    )


def respond(gateway, stream, child, request):
    """Intercept only the oc-box index; all native assets/API/WS pass untouched."""
    header, _, body = request.partition(b"\r\n\r\n")
    first = header.split(b"\r\n", 1)[0].split(b" ")
    if len(first) != 3:
        return False
    if first[1].split(b"?", 1)[0] not in tuple(
        path.encode() for path in (INDEX, SCRIPT, STYLE, CATALOG, SELECTION, HISTORY)
    ):
        return False
    target = urlsplit(first[1].decode("ascii"))
    if first[0] != b"GET" or body or len(request) > 16384:
        _reply(stream, "400 Bad Request", b"Invalid session index request.")
        return True
    fields = {}
    for line in header.split(b"\r\n")[1:]:
        name, separator, value = line.partition(b":")
        name, value = name.lower(), value.strip()
        if (
            not separator
            or name in fields
            or re.fullmatch(rb"[A-Za-z0-9-]+", name) is None
            or any(byte < 32 or byte == 127 for byte in value)
        ):
            _reply(stream, "401 Unauthorized", b"Invalid first-party request.")
            return True
        fields[name] = value
    # Native cookie validation below remains authoritative. Reject cross-origin
    # browser fetches even when localhost ports are considered same-site.
    referer = fields.get(b"referer")
    if (
        fields.get(b"origin", gateway.url.encode()) != gateway.url.encode()
        or fields.get(b"sec-fetch-site", b"none") not in (b"none", b"same-origin")
        or fields.get(b"content-length", b"0") != b"0"
        or b"transfer-encoding" in fields
        or b"upgrade" in fields
        or (
            referer is not None
            and (lambda u: u.scheme + "://" + u.netloc)(
                urlsplit(referer.decode("ascii"))
            )
            != gateway.url
        )
    ):
        _reply(stream, "401 Unauthorized", b"Invalid first-party request origin.")
        return True
    if not _authenticated(child, header, gateway.url.removeprefix("http://")):
        _reply(
            stream, "401 Unauthorized", b"Use the native one-use pairing link first."
        )
        return True
    try:
        # Python 3.10 treats an empty query as a bad field in strict mode.
        query = (
            parse_qs(target.query, keep_blank_values=True, strict_parsing=True)
            if target.query
            else {}
        )
        if set(query) - {"session"} or (
            "session" in query and len(query["session"]) != 1
        ):
            raise ValueError("Invalid shell query.")
        if target.path in (SCRIPT, STYLE):
            if query:
                raise ValueError("Invalid asset query.")
            name, content_type = (
                ("web_sessions.js", "text/javascript; charset=utf-8")
                if target.path == SCRIPT
                else ("web_sessions.css", "text/css; charset=utf-8")
            )
            _reply(
                stream,
                "200 OK",
                (Path(__file__).parent / name).read_bytes(),
                content_type,
            )
            return True
        if target.path == HISTORY:
            if __package__:
                from . import web_history
            else:
                import web_history
            try:
                result = (
                    web_history.catalog(gateway.box, gateway.data, gateway.connection)
                    if not query
                    else web_history.selection(
                        gateway.box,
                        gateway.data,
                        gateway.connection,
                        query["session"][0],
                    )
                )
            except web_history.CatalogError:
                _reply(
                    stream,
                    "503 Service Unavailable",
                    b"Workspace history could not be verified.",
                )
                return True
            _reply(
                stream,
                "200 OK",
                json.dumps(result, ensure_ascii=True).encode(),
                "application/json",
            )
            return True
        report = selection_report(gateway)
        try:
            sessions, status = reported_open_sessions(gateway, report)
        except (
            gateway.box.BoxError,
            native_tabs.TabError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            subprocess.SubprocessError,
        ):
            # Explicit existing private IDs do not depend on readable TUI state.
            # No automatic/default target is admitted on this fallback path.
            explicit = (
                gateway.connection.get("web_session_override") is not None
                or "session" in query
            )
            if target.path != INDEX and (target.path != SELECTION or not explicit):
                raise
            sessions, status = [], "unavailable"
            report = {"status": "unavailable"}
        if target.path == SELECTION:
            if set(query) != {"session"}:
                raise ValueError("Missing selected session.")
            info = explicit_session(
                gateway.box, gateway.data, gateway.connection, query["session"][0]
            )
            selected = catalog(gateway.url, [info], "explicit")["sessions"][0]
            selected["activeRoot"] = root_member(
                gateway, info, {item["id"] for item in sessions}
            )
            _reply(
                stream,
                "200 OK",
                json.dumps(selected, ensure_ascii=True).encode(),
                "application/json",
            )
            return True
        if target.path == CATALOG:
            _reply(
                stream,
                "200 OK",
                json.dumps(
                    catalog(gateway.url, sessions, status), ensure_ascii=True
                ).encode(),
                "application/json",
            )
            return True
        state = initial_state(
            gateway, sessions, status, query.get("session", [None])[0], report=report
        )
        body = document(
            gateway.url,
            sessions,
            status,
            state,
            addon_scripts=getattr(gateway, "shell_addon_scripts", ()),
            workspace_name=_workspace_name(gateway.data),
        )
    except (
        gateway.box.BoxError,
        native_tabs.TabError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ):
        _reply(
            stream,
            "503 Service Unavailable",
            error_document(),
            "text/html; charset=utf-8",
        )
        return True
    _reply(
        stream,
        "200 OK",
        body,
        "text/html; charset=utf-8",
    )
    return True
