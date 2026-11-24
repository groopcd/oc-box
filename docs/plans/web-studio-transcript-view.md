# Studio read-only transcript view (source only)

The view ports the center's `.chat-head`, `.chat-body`, `.transcript`,
`.message`, `.by` and `.avatar` from the exact approved
`21e966c:docs/mockups/web-manager.html`. The 760 px centered body, 66-character
message measure, typography, avatars and user bubble come from that design.
The messages are a static ordered list, not a live log. The mockup's demo
messages, composer, Send, tool cards, file controls and session actions are
not part of this view, so this is not full center parity.

The view consumes only the verified [read-only transcript projection]
(web-studio-conversation.md). It displays the selected title and literal
text in the provided order. “Earlier messages may not be shown” reflects
`hasEarlier`, which is conservative. “Message clipped” marks a row with
`truncated: true`; there is no title clipping flag. An empty projection is
distinct from an unavailable one. The native UI and terminal still own
composition, files, shell, tools, forms, permissions and live updates.

## Integration seam

`src/oc_box/web_studio_transcript.mjs` and its matching CSS are not served or
loaded by any shell. The caller must supply a dedicated, initially empty
`<div id="workspace-studio-transcript"></div>` in the first-party document,
outside the native frame and existing host controls. The view owns only that
slot; it never queries, reloads, replaces or edits the native frame. Keep
native Home, browser actions and the manager's no-chat boundary unchanged.
When placing the center heading, keep the existing rail/inspector controls
and avoid duplicate headings.

1. Mount once with `createStudioTranscriptView(slot, readSelection)`.
   `readSelection()` must synchronously return the trusted shell's current
   `{sessionID, generation}` or `null` when the exact selection is not valid.
   The shell—not a catalog row or event payload—owns that value.
2. Invalidate the previous view with `view.clear()` on every observed
   selection/route/document/owner invalidation and when leaving the transcript
   or closing admission. Advance the generation even on an observed
   away-and-back transition to the same ID. The component does not observe
   iframe navigation, admission or page lifecycle on its own.
3. Before starting a read, call `view.begin()`. A non-null result is a
   one-use ticket carrying the exact selected ID and generation. After the
   independently authorized read, call `view.commit(ticket, projection)` (or
   pass `null` for unavailable). It accepts only the latest ticket, rechecks
   live selection before validation and insertion, and discards late results
   without overwriting newer content. Do not retain, log or store projections
   beyond the current read and view.

There is no fetch, endpoint, listener, browser storage or credential handling
in the component. A future route must authorize the browser and exact selected
session, enforce the private owner lease, revocation and cancellation through
delivery, and cap bytes **before** parsing JSON. View validation checks the
display contract again; selection tickets are not authentication or proof of
lease validity. Do not serve or install these assets on any route until that
integration is separately authorized and reviewed.

## Checks

```sh
node --test tests/web_studio_transcript_dom.mjs
uv run --no-project --with playwright python -B \
  tests/smoke_web_studio_transcript_browser.py --chromium /path/to/chromium
```

The tests use synthetic bounded projections. The browser check measures
layout and typography, verifies literal rendering and clipping indicators,
and observes that a synthetic sibling iframe and controls stay untouched.
It is not a native, HTTPS, integrated browser or live Podman acceptance test.
