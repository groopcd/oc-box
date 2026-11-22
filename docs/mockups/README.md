# Browser workspace design preview

Open [`web-manager.html`](web-manager.html) with the sibling `tui/` directory
in place. It runs as a local file: there is no server, dependency installation,
or live OpenCode, host manager or container connection.

- **Studio** keeps chat, sessions and the inspector side by side. Drag a
  divider, or focus it and use Left/Right (Shift for larger steps, Home/End for
  bounds). Double-click or press Enter on a divider to restore its default.
  Pane widths are remembered in memory while comparing views.
- **Dashboard** puts workspace cards first. **Chat focus** hides surrounding
  panes until requested. On smaller screens, overlays and stacked panels do not
  have splitters.
- **Full TUI** contains no chat. It shows the actual Textual compositor’s
  Status, Settings, Sessions, Terminals, Logs, Storage and Backup/restore views.
  Use the browser buttons or the 1–7 keys to navigate; switch to 100% to pan
  or scroll a view without scaling. Controls in the captures are not interactive.

The [capture manifest](tui/manifest.json) records the synthetic fixture and
source hashes. The seven captures were generated from development base
`5b13a8364835d7519d183b2c509e88be2df6e342` using
`tools/capture_tui_docs.py` with an isolated output directory; no old
documentation screenshots were replaced. They do not establish live browser
TUI or external-host acceptance.

A future live Full TUI should reuse the existing host-side Textual process
rather than implement a second manager in HTML. Access to that host process
would need separate explicit authorization; a workspace OpenCode pairing is
not authority to browse host catalogs, run a host terminal, or manage backups.
