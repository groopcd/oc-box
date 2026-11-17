# Common web shell v1

The first-party shell at `/auth/oc-box-sessions` mirrors verified native TUI tab
membership from one existing private workspace service. It owns exactly one
outer tabstrip and one unmodified native app iframe, `#native`. Native browser
internal tabs are separate client-local state, not synchronized TUI membership.

Initial selection is resolved by `web_sessions.resolve_initial_selection`:
explicit `oc-box web --session ID` wins, then a URL-selected existing private
session, then a configured reader's verified native report. Without a configured
reader, a launcher-declared ID may be used only if it is an actual verified TUI
member, labeled as a declaration rather than current focus. No first-tab,
timestamp, activity-map or history guess is used. Multiple TUI clients are not
resolved magically. An unknown initial target leaves the frame unselected with
explicit tab choices and native Home navigation.
An explicit existing private ID can still be shown if TUI membership is
unavailable; the open count is then unknown, not a fabricated zero/open row.

`Gateway(..., selection_snapshot_reader=read_snapshot)` is an injectable,
trusted-code-only initial-selection seam. It calls
`read_snapshot(box, data, connection)` for an authenticated document/catalog
request. The default is `None` and performs no focus/report lookup.
Production `web_session()` always supplies `ui_state.read_snapshot`; the `None`
default is for direct Gateway consumers, not a stale-owner selection fallback.
The native reader returns a frozen `Snapshot` with `status`, exact `selected`
conversation, ordered root `tabs`, separately verified `activeRoot`, and `source`.
A valid snapshot supplies the full tab membership, not merely a selection guess
against older persisted membership. Titles are fetched through that same private
service. When the exact route is a child, the iframe opens that child while the
outer strip highlights its root; the child is not fabricated as another open tab.
Explicit CLI/URL selections still win without discarding reported membership.

The original trusted dictionary seam
`{"status": "verified", "selectedSessionID": ID, "source": LABEL}` remains supported
for direct catalog members only. Unavailable/malformed/error reports leave
selection explicit, without falling back to an old owner's declaration. Saved
membership may still be displayed when no report is available. Helper installation
and client binding are described in [ui-state.md](ui-state.md). Their native-only
checks do not by themselves establish composed-browser or host acceptance.

An explicit TUI-tab click selects the native ID route, updates the shell query
and ARIA highlight, and does not send prompts or change permissions/location.
Native SPA URL changes are observed through standard same-origin `location`,
`load`, `popstate`/`hashchange` and a one-second timer. A different ID must pass
read-only identity verification in this same private DB before being highlighted.
Foreign server prefixes, Home, drafts and cross-origin frames assert no active
TUI session. No private stores, native DOM/composer selectors or history-method
patches are used.

Refresh is explicit and non-overlapping with URL-verification requests. It
changes membership controls, never iframe `src` or the loaded draft. A selected
ID that disappears from TUI membership remains visible as a retained browser
selection outside the TUI-open count. Explicit tab changes and whole-page reloads
can navigate/reload native content; draft persistence across those actions is
the native client's responsibility.

## Optional addon boundary

The default fix has no addon enabled or imported. Trusted launcher code may set
`Gateway(..., shell_addon_scripts=(...))` (or its trusted
`Gateway.shell_addon_scripts` default) to the tuple
`("/auth/oc-box-attachments.js",)`. That separately implemented feature owns and
authenticates its script route. Browser input/query parameters cannot set this
hook. Do not introduce a wrapper around this shell, another iframe, or another
outer TUI tabstrip.

After common script initialization:

- `window.ocBoxWebShell.version === 1`.
- `.frame` is the sole `#native` iframe.
- `.controls` is the optional `#session-controls` container.
- `.status` is the separate `#addon-status` status area.
- `.selection()` returns a frozen `{sessionID, nativeRoute, activeRoot, member, origin}` or
  `null`. It returns null during pending frame loads or when current public frame
  URL does not match the verified selection. This is not native composer
  readiness, TUI focus, execution inactivity or grant authorization.
  `sessionID` remains the exact routed child when applicable; nullable
  `activeRoot` is a separately verified member, never a replacement route.
- `.refresh()` requests an explicit membership refresh without changing frame
  content.
- Window `oc-box:selection-changed` and `oc-box:frame-ready` CustomEvents carry
  that same selection object or null in `detail`. Frame-ready means a browser
  document load, not a native application/composer readiness guarantee.

Addons must recheck selection at user actions and retain their own consent,
activity, one-use/session-binding and admission checks. No prompt, file bytes,
draft state or native-private object crosses this event/API boundary. No iframe
postMessage or native model output selects host resources.

All shell documents, JS, CSS, catalog and selection-verification routes require
native browser authorization and same-origin request boundaries. They remain
under `/auth/` to bypass native's service-worker navigation fallback. The native
session route helper remains separate from the shell entry route. Response-close
transport policy and WebSocket/streaming behavior are unchanged.
