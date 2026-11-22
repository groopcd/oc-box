# Browser workspace design preview

Open [`web-manager.html`](web-manager.html) as a local file. There are no
companion files, dependencies, network requests or live service connections.

- **Studio** keeps chat, sessions and the inspector side by side. Drag a
  divider, or focus it and use Left/Right (Shift for larger steps, Home/End for
  bounds). Double-click or press Enter on a divider to restore its default.
  Pane widths are remembered in memory while comparing views. Either pane can
  take the space up to the other pane and chat’s minimum widths; if needed,
  the other pane temporarily gives up space.
- **Dashboard** puts workspace cards first. **Chat focus** hides surrounding
  panes until requested. On smaller screens, overlays and stacked panels do not
  have splitters.
- **Full TUI** is a browser adaptation of the seven manager views: Status,
  Settings, Sessions, Terminals, Logs, Storage and Backup/restore. It has a
  single top tab bar and no chat pane or terminal-style navigation sidebar.
  Use the tabs, or focus the tab bar and use arrow keys or 1–7.
  - **Terminals:** Resize with the corner or the width and height controls,
    float the panel over other manager views, or dock it again. Attach,
    detach (`Ctrl+]` while focused) and reattach to two simulated sessions.
  - **Backup/restore:** Includes full/portable workspaces, host settings and
    encrypted vault, a separate native OpenCode export/restore choice, session
    trees, and Advanced age recipient/identity controls. Inspect a sample
    archive layout to enable eligible restore options. All paths are fixtures;
    there are no real file pickers, credential fields, or archive operations.

A live browser manager would need separate authorization for host operations.
A workspace OpenCode pairing does not grant access to host catalogs,
terminals, configuration, or backups. This prototype does not implement or
validate that live integration.
