# Common web shell v1

The first-party shell at `/auth/oc-box-sessions` mirrors verified native TUI tab
membership from one existing private workspace service. It owns exactly one
outer tabstrip and one unmodified native app iframe, `#native`. Native browser
internal tabs are separate client-local state, not synchronized TUI membership.

Initial selection is resolved by `web_sessions.resolve_initial_selection`:
explicit `oc-box web --session ID` wins, then a URL-selected existing private
session, then the launcher-declared ID only if it is an actual verified TUI
member. None is claimed to be current native TUI focus. No first-tab, timestamp,
activity-map or history guess is used. Multiple TUI clients are not resolved
magically. An unknown initial target leaves the frame unselected with explicit
tab choices and native Home navigation.
An explicit existing private ID can still be shown if TUI membership is
unavailable; the open count is then unknown, not a fabricated zero/open row.

`Gateway(..., selection_snapshot_reader=read_snapshot)` is an injectable,
trusted-code-only initial-selection seam. It calls
`read_snapshot(box, data, connection)` only for initial HTML with no explicit
CLI/URL selection. The default is `None` and performs no focus/report lookup.
The eventual helper adapter must normalize its verified evidence into
`{"status": "verified", "selectedSessionID": ID, "source": LABEL}`; only a verified
private catalog member is currently admitted by this normalized seam. Other
fields (`orderedtabs`, `activeRoot`, etc.) are not guessed into focus.
Unavailable/malformed/error reports leave selection explicit, without falling
back to an old owner's declaration. The helper's actual schema, client binding,
child/root interpretation and multi-client guarantees remain integration work,
not an accepted focus implementation in this branch.

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
- `.selection()` returns a frozen `{sessionID, nativeRoute, member, origin}` or
  `null`. It returns null during pending frame loads or when current public frame
  URL does not match the verified selection. This is not native composer
  readiness, TUI focus, execution inactivity or grant authorization.
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
