# First-party Studio shell composition (not served)

The existing shell owns the masthead, session rail, one conversation heading
with drawer controls, inspector, and manager. An internal renderer opt-in adds
two dedicated, empty mounts in a hidden center. It does not insert assets or
bootstrap scripts. The first-party center uses the existing wide and narrow
layouts; it uses the real [transcript](web-studio-transcript-view.md) and
[composer](web-studio-composer-view.md), not the approved demo's sample data or
controls. The ordinary document, `web_sessions.js`, `web_sessions.css`, asset
routes, and gateway are unchanged when the opt-in is absent.

The flat installer, sdist and wheel carry the six explicitly listed Studio
CSS/MJS files as static bytes. Packaging them does not serve or activate them.

## Trusted handoff

1. A separately reviewed composition must explicitly call the internal
   renderer `web_sessions.document(..., first_party_studio=True)`. It cannot
   combine this option with the native-frame attachment addon. There is no
   request parameter, default, or served route that enables it.
2. The caller must install the reviewed `web_studio_shell.css` **after**
   `web_sessions.css`, keeping the fixed relative CSS imports intact. It
   must load `web_studio_shell.mjs` and its fixed relative JavaScript imports,
   then call `createStudioShellView(root, controller)` in the first-party
   document **after** the existing `ocBoxWebShell` version-1 object is ready.
   The bridge requires the original shell's iframe and uses its checked
   live selection API. It does not obtain authority from that API. Missing
   mounts, styles, or a suitable controller leave the native fallback visible.
   No asset publication or endpoint shape is assumed by this slice.
3. Use a preauthorized, same-document controller with these narrow methods:

   | Method | Contract |
   |---|---|
   | `readSelection()` | Synchronous `{sessionID, generation, writable}` or `null`. The ID must be the exact verified live selection. Generation is a nonnegative safe integer increasing for every newly admitted scope, even when returning to the same ID. Only `writable: true` and a supplied `send` enable the composer. |
   | `readTranscript({sessionID, generation})` | Resolve within the controller's bound to the existing transcript projection or an unavailable result. The bridge checks exact selection and generation before dispatch and again when applying it. |
   | `send({sessionID, generation, text})` | Optional scoped adapter bound to one explicit Send. Return `"queued"` only for verified native acknowledgement, `"refused"` only when no native POST was attempted, or `"unconfirmed"`. The authority and transport checks still belong to the controller. |

4. `await view.present()` is explicit. While the read is pending, the native
   fallback remains visible. Only a valid current projection reveals the
   first-party view; a non-writable scope shows only the transcript. A
   successful repeated `present()` for the current generation leaves its
   draft and transcript untouched. A failed, pending, or stale call returns
   `false`; there is at most one transcript read in flight and no automatic
   retry. Once a generation is accepted for a read, it cannot be reused
   after failure or invalidation.
5. On **every observed** selection/route/document, visibility, owner or
   admission invalidation, revoke the old snapshot, call `view.invalidate()`
   synchronously, and signal cancellation of old work to the controller.
   Re-present only with new, verified authority and a higher generation,
   after any prior read settles. The bridge neither observes navigation nor
   subscribes to global events. Events may prompt the caller to invalidate;
   they are never permission to mount, read, or Send. The caller owns precise
   pre-disclosure checks, cancellation, revocation, and deadlines. Narrow
   layout drawers make the chat inert rather than changing conversation.

The fallback iframe is never moved, rewritten, reloaded, or edited by the
composition. It remains mounted and is hidden **by the opted-in parent CSS**
only while the first-party center is visible. Invalidation clears the mounts
and re-exposes the same fallback, subject to the existing shell's own
selection and layout. Native Home and normal native navigation retain their
existing behavior.

## Synthetic Chromium check

```sh
PYTHONPATH=src uv run --offline --no-project --with playwright python -B \
  tests/smoke_web_studio_shell_browser.py --chromium /path/to/chromium
```

Chrome Headless Shell 159.0.8154.6 verified the unchanged shell assets with
intercepted synthetic pages. At 2269 × 1398 the five pane positions and widths
and the composer's comparable relative position and dimensions matched the
approved `21e966ce` mockup. At 390 px the existing on-demand pane made the chat
inert without losing its draft or a same-selection receipt. Pending or
malformed reads, stale generations, read-only scopes and lifecycle transitions
preserved the expected fallback and never fabricated a Send. The native
fixture frame kept its identity, attributes and draft through presentation and
fallback. The only post-bootstrap request was the existing shell's explicit
navigation to a different synthetic iframe session. No live owner, provider,
native admission, one-use/named route, or Podman was used.
